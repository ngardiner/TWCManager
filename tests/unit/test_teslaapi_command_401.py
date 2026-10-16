"""
A 401 on a vehicle command or wake. An expired token is refreshed and the
request sent once more; any other 401 clears the token, so commands stop
using it and the next provider carries them. (A 401 for a token another
thread replaced while the request was out: test_provider_outcomes_cloud.)
"""

import re
import time
from unittest.mock import Mock

import pytest

from TWCManager.Vehicle.Reconcile.Outcome import DONE, REFUSED, SATISFIED, Outcome
from tests.unit.teslatokens import FLEET_REFRESH_URL, fleet_token, new_api

OPAQUE_REFRESH = "NA_0123456789abcdef0123456789abcdef"
OK = {"json": {"response": {"result": True, "reason": ""}}}
EXPIRED = {"status_code": 401, "text": "token expired (401)"}
VIN = "5YJ3E1EA1KF000001"
DEAD = {"status_code": 401, "text": '{"error": "invalid bearer token"}'}


@pytest.fixture
def api(monkeypatch):
    import TWCManager.Vehicle.TeslaAPI as teslaapi

    monkeypatch.setattr(teslaapi.time, "sleep", lambda s: None)
    a = new_api()
    a.addVehicle({"id": 1, "vin": VIN, "display_name": "Car1"})
    a.getCarApiVehicles()[0].lastAPIAccessTime = time.time()
    return a


def with_refresh_token(api, requests_mock):
    api.storeToken(fleet_token(jti="old"), OPAQUE_REFRESH)
    new = fleet_token(jti="new")
    refresh = requests_mock.post(
        FLEET_REFRESH_URL,
        json={"access_token": new, "refresh_token": "R2", "expires_in": 28800},
    )
    return new, refresh


def bearer(request):
    return request.headers["Authorization"].split(" ", 1)[1]


def command(api, name):
    v = api.getCarApiVehicles()[0]
    if name == "wake_up":
        outcome, response = api._paidPost(v, api.fleetSlot, "wake_up", purpose="wake")
        return Outcome(DONE) if outcome is None else outcome
    body = {
        "set_charging_amps": {"charging_amps": 16},
        "set_charge_limit": {"percent": 80},
    }.get(name)
    return api.command(v, name, body)


def failed(result):
    return result.kind not in (DONE, SATISFIED)


COMMANDS = ["charge_stop", "set_charging_amps", "wake_up", "set_charge_limit"]


def url(name):
    return re.compile(r".*/" + VIN + "/(command/)?" + name + "$")


@pytest.mark.parametrize("name", COMMANDS)
def test_expired_token_is_refreshed_and_command_retried(api, requests_mock, name):
    new, refresh = with_refresh_token(api, requests_mock)
    ok = {"json": {"response": {"state": "online"}}} if name == "wake_up" else OK
    cmd = requests_mock.post(url(name), [EXPIRED, ok])

    result = command(api, name)

    assert refresh.call_count == 1
    assert cmd.call_count == 2
    assert bearer(cmd.request_history[1]) == new
    assert not failed(result)


@pytest.mark.parametrize("name", COMMANDS)
def test_dead_token_is_cleared(api, requests_mock, name):
    api.storeToken(fleet_token(), "")
    cmd = requests_mock.post(url(name), [DEAD])

    result = command(api, name)

    assert result == Outcome(REFUSED, "401")
    assert cmd.call_count == 1
    assert api.fleetSlot.empty()
    assert {"cmd": "saveSettings"} in api.master.tasks
    # With no command token left, the next command is refused without a
    # request, so the next provider carries it
    assert api.slotFor("command") is None


@pytest.mark.parametrize("name", COMMANDS)
def test_expired_after_refresh_is_not_retried_again(api, requests_mock, name):
    new, refresh = with_refresh_token(api, requests_mock)
    cmd = requests_mock.post(url(name), [EXPIRED, EXPIRED, EXPIRED, EXPIRED])

    result = command(api, name)

    assert failed(result)
    assert refresh.call_count == 1
    assert cmd.call_count == 2


def test_command_403_does_not_pause_reads(api, requests_mock):
    api.storeToken(fleet_token(), "")
    requests_mock.post(url("set_charging_amps"), status_code=403, text="")

    assert command(api, "set_charging_amps") == Outcome(REFUSED, "403")

    assert api.fleetSlot.pausedUntil == 0
    assert not api.fleetSlot.empty()
