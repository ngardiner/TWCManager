"""
A 401 on a vehicle command (#689). An expired token is refreshed and the
command retried once; any other 401 clears the token, so commands stop
using it and VehiclePriority falls back to the next module.
"""

import re
import time
from unittest.mock import Mock

import pytest

from tests.unit.teslatokens import FLEET_REFRESH_URL, fleet_token, new_api

OPAQUE_REFRESH = "NA_0123456789abcdef0123456789abcdef"
OK = {"json": {"response": {"result": True, "reason": ""}}}
EXPIRED = {"status_code": 401, "text": "token expired (401)"}
DEAD = {"status_code": 401, "text": '{"error": "invalid bearer token"}'}


class _NoThread:
    daemon = False

    def start(self):
        pass


@pytest.fixture
def api(monkeypatch):
    import TWCManager.Vehicle.TeslaAPI as teslaapi

    monkeypatch.setattr(teslaapi, "Thread", lambda *a, **k: _NoThread())
    monkeypatch.setattr(teslaapi.time, "sleep", lambda s: None)
    a = new_api()
    a.addVehicle({"id": 1, "vin": "V1", "display_name": "Car1"})
    v = a.getCarApiVehicles()[0]
    v.atHome = True
    v.lastAPIAccessTime = time.time()
    v.chargingState = "Charging"
    v.update_charge = Mock(return_value=True)
    v.update_location = Mock(return_value=True)
    a.applyChargeLimit = Mock()
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
    if name == "charge_stop":
        return api.car_api_charge({"cmd": "charge", "charge": False, "vin": "V1"})
    if name == "set_charging_amps":
        return api.setChargeRate(16, v)
    if name == "wake_up":
        return api.wakeVehicle(v)
    if name == "set_charge_limit":
        return v.apply_charge_limit(80)


COMMANDS = ["charge_stop", "set_charging_amps", "wake_up", "set_charge_limit"]


def url(name):
    return re.compile(r".*/V1/(command/)?" + name + "$")


@pytest.mark.parametrize("name", COMMANDS)
def test_expired_token_is_refreshed_and_command_retried(api, requests_mock, name):
    new, refresh = with_refresh_token(api, requests_mock)
    ok = {"json": {"response": {"state": "online"}}} if name == "wake_up" else OK
    cmd = requests_mock.post(url(name), [EXPIRED, ok])

    result = command(api, name)

    assert refresh.call_count == 1
    assert cmd.call_count == 2
    assert bearer(cmd.request_history[1]) == new
    assert result not in (False, "error")


@pytest.mark.parametrize("name", COMMANDS)
def test_dead_token_is_cleared(api, requests_mock, name):
    api.storeToken(fleet_token(), "")
    cmd = requests_mock.post(url(name), [DEAD])

    result = command(api, name)

    assert result in (False, "error")
    assert cmd.call_count == 1
    assert api.fleetSlot.empty()
    assert {"cmd": "saveSettings"} in api.master.tasks
    # With no command token left, the next command is refused without a
    # request, so VehiclePriority moves on to the next module.
    assert api.readOnlyFor(api.getCarApiVehicles()[0])


@pytest.mark.parametrize("name", COMMANDS)
def test_expired_after_refresh_is_not_retried_again(api, requests_mock, name):
    new, refresh = with_refresh_token(api, requests_mock)
    cmd = requests_mock.post(url(name), [EXPIRED, EXPIRED, EXPIRED, EXPIRED])

    result = command(api, name)

    assert result in (False, "error")
    assert refresh.call_count == 1


def test_set_charging_amps_401_is_not_success(api, requests_mock):
    # Before #689 the 401 body, a non-empty dict, read as success.
    api.storeToken(fleet_token(), "")
    requests_mock.post(url("set_charging_amps"), [DEAD])

    assert api.setChargeRate(16, api.getCarApiVehicles()[0]) is False


def test_token_replaced_while_command_ran_is_kept(api, requests_mock):
    # A 401 for a token another thread has already replaced must not clear
    # the new one.
    api.storeToken(fleet_token(jti="old"), "")
    sent = api.fleetSlot.accessToken
    api.storeToken(fleet_token(jti="pasted"), "", "pasted")
    response = Mock(status_code=401, text="invalid bearer token")

    assert api.slotCommandFailed(api.fleetSlot, response, sent) is True
    assert not api.fleetSlot.empty()


def test_command_403_does_not_pause_reads(api, requests_mock):
    api.storeToken(fleet_token(), "")
    requests_mock.post(url("set_charging_amps"), status_code=403, text="")

    api.setChargeRate(16, api.getCarApiVehicles()[0])

    assert api.fleetSlot.pausedUntil == 0
    assert not api.fleetSlot.empty()
