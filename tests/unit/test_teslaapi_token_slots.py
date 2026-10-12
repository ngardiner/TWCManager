"""
TeslaAPI holds two token slots, one for an Owner API token and one for a
Fleet API token. Reads use the Owner slot when it is usable and the Fleet
slot otherwise; commands use only the Fleet slot.
"""

import json
import re
import time

import jwt
import pytest
import requests
import requests_mock as rm

from tests.unit.teslatokens import (
    AUTH_TOKEN_URL,
    FLEET_NA_URL,
    FLEET_REFRESH_URL,
    OWNER_API_URL,
    add_home_vehicle,
    fleet_token,
    new_api,
    no_threads,
    owner_token,
)
from tests.unit.test_teslaapi_refresh import (
    OPAQUE_REFRESH,
    owner_refresh_token,
    sync_on,
    token_response,
)
from tests.unit.test_teslamate_token_sync import master_with_settings

VEHICLE_DATA = {
    "response": {
        "drive_state": {"latitude": 37.0, "longitude": -122.0},
        "charge_state": {"battery_level": 55, "charge_limit_soc": 80},
        "vehicle_state": {"vehicle_name": "Car"},
    }
}
LISTING = {"count": 1, "response": [{"id": 1, "vin": "VIN1", "display_name": "Car"}]}
CMD_OK = {"response": {"result": True, "reason": ""}}


def exp(token):
    return jwt.decode(token, options={"verify_signature": False})["exp"]


def store_both(api, ownerRefresh=None, fleetRefresh=None, expires_in=8 * 60 * 60):
    owner, fleet = owner_token(expires_in=expires_in), fleet_token(
        expires_in=expires_in
    )
    assert api.storeToken(owner, ownerRefresh) == "success"
    assert api.storeToken(fleet, fleetRefresh) == "success"
    return owner, fleet


def fleet_calls(requests_mock):
    return requests_mock.register_uri(
        rm.ANY, re.compile(re.escape(FLEET_NA_URL)), json={}
    )


def test_pasted_tokens_land_in_slot_by_type():
    api = new_api()
    owner, fleet = store_both(api)
    assert api.ownerSlot.tokenType == "owner" and api.fleetSlot.tokenType == "fleet"
    assert api.ownerSlot.accessToken == owner
    assert api.fleetSlot.accessToken == fleet
    assert api.ownerSlot.baseURL == OWNER_API_URL
    assert api.fleetSlot.baseURL == FLEET_NA_URL


def test_store_token_results():
    api = new_api()
    assert api.storeToken("not-a-jwt-at-all") == "unknown_type"
    assert api.storeToken("", "") == "error"
    assert api.ownerSlot.empty() and api.fleetSlot.empty()
    sync_on(api)
    # A free slot takes a pasted token while sync is on; a slot with a live
    # synced token does not.
    assert api.storeToken(owner_token(), "", "synced") == "success"
    assert api.storeToken(owner_token(jti="p")) == "token_sync"
    assert api.ownerSlot.source == "synced"


def test_reads_use_owner_when_usable(no_threads, requests_mock):
    api = new_api()
    owner, _ = store_both(api)
    vehicle = add_home_vehicle(api)
    data = requests_mock.get(OWNER_API_URL + "/VIN1/vehicle_data", json=VEHICLE_DATA)
    fleet = fleet_calls(requests_mock)

    assert vehicle.update_vehicle_data() is True

    assert data.call_count == 1
    assert data.last_request.headers["Authorization"] == "Bearer " + owner
    assert fleet.call_count == 0


def test_reads_use_fleet_without_owner(no_threads, requests_mock):
    api = new_api()
    token = fleet_token()
    api.storeToken(token)
    vehicle = add_home_vehicle(api)
    data = requests_mock.get(FLEET_NA_URL + "/VIN1/vehicle_data", json=VEHICLE_DATA)

    assert vehicle.update_vehicle_data() is True

    assert data.call_count == 1
    assert data.last_request.headers["Authorization"] == "Bearer " + token


def test_vehicle_list_from_owner_products_when_both_present(no_threads, requests_mock):
    api = new_api()
    store_both(api)
    products = requests_mock.get(OWNER_API_URL[:-8] + "products", json=LISTING)
    fleetList = requests_mock.get(FLEET_NA_URL, json=LISTING)

    assert api.car_api_available() is True

    assert products.call_count == 1
    assert fleetList.call_count == 0
    assert api.getVehicleCount() == 1


def test_commands_use_fleet_only(no_threads, requests_mock):
    api = new_api()
    _, fleet = store_both(api)
    vehicle = add_home_vehicle(api)
    cmd = requests_mock.post(
        FLEET_NA_URL + "/VIN1/command/set_charging_amps", json=CMD_OK
    )

    assert api.setChargeRate(10, vehicle) == CMD_OK

    assert cmd.call_count == 1
    assert cmd.last_request.headers["Authorization"] == "Bearer " + fleet


@pytest.mark.parametrize(
    "fleet",
    [None, fleet_token(scp=["openid", "vehicle_device_data"])],
    ids=["owner-only", "fleet-without-command-scope"],
)
def test_command_refused_without_usable_fleet(no_threads, requests_mock, fleet):
    api = new_api()
    api.storeToken(owner_token())
    if fleet:
        api.storeToken(fleet)
    vehicle = add_home_vehicle(api)
    anyCall = requests_mock.register_uri(rm.ANY, rm.ANY, json={})

    assert api.readOnly is True
    assert api.setChargeRate(10, vehicle) is False
    assert api.wakeVehicle(vehicle) is False
    assert api.car_api_charge({"charge": True}) == "error"
    assert api.applyChargeLimit(80) == "error"
    assert vehicle.apply_charge_limit(80) is False

    assert anyCall.call_count == 0
    assert api.getCarApiLastErrorTime() == 0
    assert vehicle.lastErrorTime == 0


def test_no_usable_slot_makes_no_requests(no_threads, requests_mock):
    api = new_api()
    vehicle = add_home_vehicle(api)
    anyCall = requests_mock.register_uri(rm.ANY, rm.ANY, json={})

    assert api.car_api_available() is False
    assert vehicle.is_awake() is False
    assert vehicle.update_vehicle_data() is False

    assert anyCall.call_count == 0
    assert api.getCarApiLastErrorTime() == 0
    assert api.readOnly is True


def test_refresh_if_needed_refreshes_each_slot_at_its_endpoint(requests_mock):
    owner = requests_mock.post(AUTH_TOKEN_URL, json=token_response(owner_token()))
    fleet = requests_mock.post(
        FLEET_REFRESH_URL,
        json={
            "access_token": fleet_token(),
            "refresh_token": "new-opaque",
            "expires_in": 28800,
        },
    )
    api = new_api()
    ownerRefresh = owner_refresh_token()
    store_both(api, ownerRefresh, OPAQUE_REFRESH, expires_in=60)

    api.refreshTokenIfNeeded()

    assert owner.call_count == 1
    assert owner.last_request.json()["client_id"] == "ownerapi"
    assert owner.last_request.json()["refresh_token"] == ownerRefresh
    assert fleet.call_count == 1
    assert fleet.last_request.json()["client_id"] == "my-client-id"
    assert fleet.last_request.json()["refresh_token"] == OPAQUE_REFRESH
    assert api.fleetSlot.refreshToken == "new-opaque"
    assert {"cmd": "saveSettings"} in api.master.tasks


def test_rejected_refresh_clears_only_that_slot(requests_mock):
    requests_mock.post(AUTH_TOKEN_URL, status_code=401, json={})
    fleetRefresh = requests_mock.post(FLEET_REFRESH_URL, json={})
    api = new_api()
    api.storeToken(owner_token(expires_in=60), owner_refresh_token())
    fleet = fleet_token()
    api.storeToken(fleet, OPAQUE_REFRESH)

    api.refreshTokenIfNeeded()

    assert api.ownerSlot.accessToken == "" and api.ownerSlot.refreshToken == ""
    assert api.fleetSlot.accessToken == fleet
    assert api.fleetSlot.refreshToken == OPAQUE_REFRESH
    assert fleetRefresh.call_count == 0


def expired_then(requests_mock, responses):
    return requests_mock.get(
        FLEET_NA_URL + "/VIN1/vehicle_data",
        [{"status_code": 401, "text": "token expired"}] + responses,
    )


def test_get_car_api_expired_401_refreshes_once_and_retries(no_threads, requests_mock):
    api = new_api()
    api.storeToken(fleet_token(jti="old"), OPAQUE_REFRESH)
    vehicle = add_home_vehicle(api)
    data = expired_then(requests_mock, [{"json": {"response": {"ok": True}}}])
    newAccess = fleet_token(jti="new")
    refresh = requests_mock.post(
        FLEET_REFRESH_URL,
        json={"access_token": newAccess, "refresh_token": "r2", "expires_in": 28800},
    )

    assert vehicle.get_car_api("VIN1/vehicle_data", checkReady=False) == (
        True,
        {"ok": True},
    )

    assert refresh.call_count == 1
    assert data.call_count == 2
    assert data.request_history[1].headers["Authorization"] == "Bearer " + newAccess


def test_get_car_api_refreshes_at_most_once(no_threads, requests_mock):
    api = new_api()
    api.storeToken(fleet_token(), OPAQUE_REFRESH)
    vehicle = add_home_vehicle(api)
    data = expired_then(
        requests_mock, [{"status_code": 401, "text": "token expired"}] * 2
    )
    refresh = requests_mock.post(
        FLEET_REFRESH_URL,
        json={"access_token": fleet_token(), "refresh_token": "r", "expires_in": 60},
    )

    assert vehicle.get_car_api("VIN1/vehicle_data", checkReady=False) == (
        False,
        None,
    )

    assert refresh.call_count == 1
    assert data.call_count == 2


def test_get_car_api_connection_error_returns_false(no_threads, requests_mock):
    api = new_api()
    api.storeToken(fleet_token())
    vehicle = add_home_vehicle(api)
    requests_mock.get(
        FLEET_NA_URL + "/VIN1/vehicle_data", exc=requests.exceptions.ConnectionError
    )

    assert vehicle.get_car_api("VIN1/vehicle_data", checkReady=False) == (
        False,
        None,
    )


def test_restore_legacy_sentinel_uses_token_exp(tmp_path):
    token = fleet_token()
    master, api, _ = master_with_settings(
        tmp_path,
        {
            "carApiBearerToken": token,
            "carApiRefreshToken": "",
            "carApiTokenExpireTime": 99999 * 99999 * 99999,
        },
    )

    master.loadSettings()

    assert api.fleetSlot.accessToken == token
    assert api.fleetSlot.expireTime == exp(token)


def test_save_and_load_keep_both_slots(tmp_path):
    master, api, _ = master_with_settings(tmp_path, {})
    owner, fleet = store_both(api, "r-o", "r-f")

    master.saveSettings()
    saved = json.loads((tmp_path / "settings.json").read_text())
    assert saved["ownerApiBearerToken"] == owner
    assert saved["carApiBearerToken"] == fleet

    master2, api2, _ = master_with_settings(tmp_path, saved)
    master2.loadSettings()

    for before, after in [
        (api.ownerSlot, api2.ownerSlot),
        (api.fleetSlot, api2.fleetSlot),
    ]:
        assert after.accessToken == before.accessToken
        assert after.refreshToken == before.refreshToken
        assert after.expireTime == before.expireTime
        assert after.tokenType == before.tokenType
    assert api2.ownerSlot.refreshToken == "r-o"
    assert api2.fleetSlot.refreshToken == "r-f"


def test_readonly_property_follows_fleet_slot():
    api = new_api()
    assert api.readOnly is True
    api.storeToken(owner_token())
    assert api.readOnly is True
    api.storeToken(fleet_token())
    assert api.readOnly is False
    api.fleetSlot.clear()
    assert api.readOnly is True
    with pytest.raises(AttributeError):
        api.readOnly = False


def test_slot_for_is_uniform_walk():
    api = new_api()
    store_both(api)
    assert api.slotFor("read") is api.ownerSlot
    assert api.slotFor("energy") is api.ownerSlot
    assert api.slotFor("command") is api.fleetSlot
    assert api.readOnly is False

    owner_only = new_api()
    owner_only.ownerSlot.setTokens(owner_token())
    assert owner_only.slotFor("command") is None
    assert owner_only.readOnly is True


def test_owner_401_clears_owner_and_next_read_uses_fleet(
    no_threads, requests_mock, caplog
):
    api = new_api()
    owner, fleet = store_both(api)
    vehicle = add_home_vehicle(api)
    ownerData = requests_mock.get(
        OWNER_API_URL + "/VIN1/vehicle_data",
        status_code=401,
        json={"error": "invalid bearer"},
    )
    fleetData = requests_mock.get(
        FLEET_NA_URL + "/VIN1/vehicle_data", json=VEHICLE_DATA
    )

    assert vehicle.update_vehicle_data() is False
    assert api.ownerSlot.accessToken == ""
    assert "Tesla Owner API token was rejected (HTTP 401); removing it." in caplog.text
    assert {"cmd": "saveSettings"} in api.master.tasks
    assert ownerData.call_count == 1

    assert vehicle.update_vehicle_data() is True
    assert fleetData.call_count == 1
    assert owner not in caplog.text and fleet not in caplog.text


def test_owner_412_pauses_for_an_hour_then_returns(
    no_threads, requests_mock, monkeypatch
):
    api = new_api()
    owner, _ = store_both(api)
    products = requests_mock.get(OWNER_API_URL[:-8] + "products", status_code=412)
    fleetList = requests_mock.get(FLEET_NA_URL, json=LISTING)
    now = time.time()

    assert api.car_api_available() is False
    assert api.ownerSlot.accessToken == owner
    assert abs(api.ownerSlot.pausedUntil - (now + 3600)) < 5
    assert products.call_count == 1

    assert api.car_api_available() is True
    assert fleetList.call_count == 1

    monkeypatch.setattr(time, "time", lambda: now + 3601)
    assert api.slotFor("read") is api.ownerSlot


def test_new_sync_ends_synced_pause():
    api = new_api()
    api.storeToken(owner_token(jti="o"), "", "synced")
    api.ownerSlot.pause()
    assert api.slotFor("read") is None
    api.storeToken(owner_token(jti="n"), "", "synced")
    assert api.slotFor("read") is api.ownerSlot


def test_synced_401_expired_clears_without_refresh(no_threads, requests_mock):
    api = new_api()
    api.storeToken(owner_token(), "", "synced")
    vehicle = add_home_vehicle(api)
    requests_mock.get(
        OWNER_API_URL + "/VIN1/vehicle_data", status_code=401, text="token expired"
    )
    refresh = requests_mock.post(AUTH_TOKEN_URL, json={})

    assert vehicle.get_car_api("VIN1/vehicle_data", checkReady=False) == (False, None)

    assert refresh.call_count == 0
    assert api.ownerSlot.accessToken == ""


@pytest.mark.parametrize("code", [503, 429])
def test_5xx_and_429_do_not_pause_or_clear(no_threads, requests_mock, code):
    api = new_api()
    owner, _ = store_both(api)
    vehicle = add_home_vehicle(api)
    requests_mock.get(OWNER_API_URL + "/VIN1/vehicle_data", status_code=code)

    assert vehicle.get_car_api("VIN1/vehicle_data", checkReady=False) == (False, None)

    assert api.ownerSlot.accessToken == owner
    assert api.ownerSlot.pausedUntil == 0
    assert api.slotFor("read") is api.ownerSlot


def test_both_paused_no_requests(no_threads, requests_mock):
    api = new_api()
    store_both(api)
    api.ownerSlot.pause()
    api.fleetSlot.pause()
    anything = requests_mock.register_uri(rm.ANY, re.compile(".*"), json={})

    assert api.car_api_available() is False

    assert anything.call_count == 0


def test_slot_failure_records_no_api_error(no_threads, requests_mock):
    api = new_api()
    store_both(api)
    requests_mock.get(OWNER_API_URL[:-8] + "products", status_code=412)

    assert api.car_api_available() is False

    assert api.getCarApiLastErrorTime() == 0
