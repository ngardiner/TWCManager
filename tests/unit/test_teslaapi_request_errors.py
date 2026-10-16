"""
TeslaAPI handling of requests that fail before or after a response.

A request that never goes out (no network, DNS failure, connection refused)
raises before any response exists, so the error handlers must not read a
status code from it.
"""

import re
from unittest.mock import Mock

import pytest
import requests

from tests.unit.teslatokens import FLEET_NA_URL, fleet_token

VIN = "5YJ3E1EA0KF000001"
NEW_ACCESS = fleet_token(jti="new")
# Any Tesla token endpoint; which host refreshes a token varies by release.
TOKEN_URL = re.compile(r"/oauth2/v3/token$")


def make_master():
    master = Mock()
    master.config = {
        "config": {"teslaApiClientID": "my-client-id"},
        "vehicle": {"teslaAPI": {"enabled": True}},
    }
    # tokenSyncEnabled() must be falsey or storeToken refuses the token
    master.tokenSyncEnabled.return_value = False
    return master


@pytest.fixture
def api():
    from TWCManager.Vehicle.TeslaAPI import TeslaAPI

    api = TeslaAPI(make_master())
    # A classified token, so requests have a base URL to go to.
    api.storeToken(fleet_token(jti="old"), "old-refresh")
    api.carApiLastErrorTime = 0
    return api


@pytest.fixture
def vehicle(api):
    from TWCManager.Vehicle.TeslaAPI import CarApiVehicle

    return CarApiVehicle(
        {"id": 1, "vin": VIN, "display_name": "Car"}, api, api.master.config
    )


class TestRefresh:
    def test_request_never_sent(self, api, requests_mock):
        requests_mock.post(TOKEN_URL, exc=requests.exceptions.ConnectionError)

        assert api.refreshSlot(api.fleetSlot) == "unreachable"

        # No 401 came back, so the tokens are kept for a later retry.
        assert api.fleetSlot.refreshToken == "old-refresh"
        assert api.getCarApiLastErrorTime() > 0

    def test_401_still_clears_tokens(self, api, requests_mock):
        requests_mock.post(TOKEN_URL, status_code=401, json={})

        assert api.refreshSlot(api.fleetSlot) == "rejected"
        assert api.fleetSlot.refreshToken == ""


class TestGetCarApi:
    PATH = VIN + "/vehicle_data"
    URL = FLEET_NA_URL + "/" + PATH

    def test_request_never_sent(self, vehicle, requests_mock):
        requests_mock.get(self.URL, exc=requests.exceptions.ConnectionError)

        assert vehicle.get_car_api(self.PATH, checkReady=False) == (False, None)

    def test_expired_token_refreshes_and_retries_with_new_token(
        self, api, vehicle, requests_mock
    ):
        data = requests_mock.get(
            self.URL,
            [
                {"status_code": 401, "text": "token expired"},
                {"status_code": 200, "json": {"response": {"ok": True}}},
            ],
        )
        requests_mock.post(
            TOKEN_URL,
            json={
                "access_token": NEW_ACCESS,
                "refresh_token": "new-refresh",
                "expires_in": 28800,
            },
        )

        ok, response = vehicle.get_car_api(self.PATH, checkReady=False)

        assert ok is True
        assert response == {"ok": True}
        assert data.call_count == 2
        assert (
            data.request_history[1].headers["Authorization"] == "Bearer " + NEW_ACCESS
        )


# One request helper and one status classifier serve every TeslaAPI request
# path. Each cell: what one request answering `status` leaves behind.

HOME_VIN = "5YJ3E1EA1KF000001"  # add_home_vehicle
API_ROOT = FLEET_NA_URL[: -len("vehicles")]
PATH_URLS = {
    "read": FLEET_NA_URL + "/" + HOME_VIN + "/vehicle_data",
    "list": FLEET_NA_URL,
    "command": FLEET_NA_URL + "/" + HOME_VIN + "/command/charge_stop",
    "energy": API_ROOT + "products",
}
TIMEOUTS = {"read": 10, "list": 10, "command": 10, "energy": 10}
REFRESH = "fleet-refresh-secret"
NO = dict(
    calls=1,
    refreshes=0,
    token="kept",
    paused=None,
    refused=set(),
    wc=False,
    backoff=None,
)


def cell(**changes):
    return dict(NO, **changes)


EXPECTED = {
    # 401 "token expired" every time, with a refresh that succeeds
    ("read", 401): cell(result=False, calls=2, refreshes=1, token="cleared"),
    ("list", 401): cell(result=False, refreshes=1, token="new"),
    ("command", 401): cell(
        result=("refused", "401"), calls=2, refreshes=1, token="cleared"
    ),
    ("energy", 401): cell(result=None, wc=True),
    ("read", 403): cell(result=False, paused="hour"),
    ("list", 403): cell(result=False, paused="hour"),
    ("command", 403): cell(result=("refused", "403"), refused={"command"}),
    ("energy", 403): cell(result=None, wc=True),
    ("read", 408): cell(result=False),
    ("list", 408): cell(result=False, backoff="module"),
    ("command", 408): cell(result=("unreachable", "408")),
    ("energy", 408): cell(result=None),
    ("read", 410): cell(result=False, paused="hour"),
    ("list", 410): cell(result=False, paused="hour"),
    ("command", 410): cell(result=("error", "410")),
    ("energy", 410): cell(result=None, wc=True),
    ("read", 412): cell(result=False, paused="latched"),
    ("list", 412): cell(result=False, paused="latched"),
    ("command", 412): cell(result=("refused", "412"), refused={"command"}),
    ("energy", 412): cell(result=None, wc=True),
    ("read", 429): cell(result=False, backoff="car"),
    ("list", 429): cell(result=False, backoff="module"),
    ("command", 429): cell(result=("error", "429"), backoff="car"),
    ("energy", 429): cell(result=None),
}


def backoff_of(api, car):
    # "car": that car alone backs off (its errorCount, not the module's);
    # "module": every car and the vehicle list do
    onCar = bool(car.lastErrorTime or car.errorCount)
    onModule = bool(api.carApiLastErrorTime or api.errorCount)
    return {(False, False): None, (True, False): "car", (False, True): "module"}.get(
        (onCar, onModule), "both"
    )


def run_path(api, path):
    if path == "read":
        car = api.carApiVehicles[0]
        return car.get_car_api(HOME_VIN + "/vehicle_data", checkReady=False)[0]
    if path == "list":
        api.carApiVehicles = []
        return api.loadVehicleList()
    if path == "command":
        return tuple(api.command(api.carApiVehicles[0], "charge_stop"))
    return api.wallConnectorVins(api.fleetSlot)


# A 401 that is not an expiry, and a request that never left (its text
# carries the token, as a wrapped header error can)
EXPECTED.update(
    {
        ("read", "401 rejected"): cell(result=False, token="cleared"),
        ("list", "401 rejected"): cell(result=False, token="cleared"),
        ("command", "401 rejected"): cell(result=("refused", "401"), token="cleared"),
        ("energy", "401 rejected"): cell(result=None, wc=True),
        ("read", "never sent"): cell(result=False),
        ("list", "never sent"): cell(result=False, backoff="module"),
        ("command", "never sent"): cell(result=("error", "RequestException")),
        ("energy", "never sent"): cell(result=None),
    }
)


@pytest.mark.parametrize("path,status", sorted(EXPECTED, key=str))
def test_every_path_classifies_status_codes(path, status, requests_mock, caplog):
    from tests.unit.teslatokens import FLEET_REFRESH_URL, add_home_vehicle, new_api
    from tests.unit.test_teslaapi_refresh import token_response

    caplog.set_level(1)
    api = new_api()
    old = fleet_token(jti="old")
    api.storeToken(old, REFRESH)
    car = add_home_vehicle(api)
    new = fleet_token(jti="new")
    refresh = requests_mock.post(FLEET_REFRESH_URL, json=token_response(new))
    if status == "never sent":
        answer = {"exc": requests.exceptions.RequestException("Bearer " + old)}
    elif status == "401 rejected":
        answer = {"status_code": 401, "text": "invalid bearer"}
    else:
        answer = {
            "status_code": status,
            "text": "token expired" if status == 401 else "{}",
        }
    hit = requests_mock.request(
        "GET" if path != "command" else "POST", PATH_URLS[path], **answer
    )
    slot = api.fleetSlot

    result = run_path(api, path)

    want = EXPECTED[(path, status)]
    paused = None
    if slot.pausedUntil == float("inf"):
        paused = "latched"
    elif slot.pausedUntil > 0:
        paused = "hour"
    token = {old: "kept", new: "new", "": "cleared"}[slot.accessToken]
    got = dict(
        result=result,
        calls=hit.call_count,
        refreshes=refresh.call_count,
        token=token,
        paused=paused,
        refused=slot.refusedPurposes,
        wc=api._wallConnectorsRefused.get("fleet") == slot.changes,
        backoff=backoff_of(api, car),
    )
    assert got == want
    assert all(r.timeout == TIMEOUTS[path] for r in hit.request_history)
    for secret in (old, new, REFRESH):
        assert secret not in caplog.text


def test_a_wake_answering_online_refreshes_the_online_cache(requests_mock, monkeypatch):
    # The wake's "online" replaces a cached "asleep" for the next 120 s: the
    # free check is not asked again
    import TWCManager.Vehicle.TeslaAPI as T
    from TWCManager.Vehicle.Reconcile.Outcome import DONE
    from tests.unit.reconcile_helpers import Clock
    from tests.unit.teslatokens import add_home_vehicle, new_api

    monkeypatch.setattr(T.time, "sleep", Mock())
    clk = Clock(5000.0)
    api = new_api()
    api.storeToken(fleet_token())
    api.now, api.wakeDelayMins = clk, 0
    car = add_home_vehicle(api)
    check = requests_mock.get(
        FLEET_NA_URL + "/" + HOME_VIN, json={"response": {"state": "asleep"}}
    )
    wake = requests_mock.post(
        FLEET_NA_URL + "/" + HOME_VIN + "/wake_up",
        json={"response": {"state": "online"}},
    )
    assert car.is_awake() is False and check.call_count == 1
    clk.t += 100
    assert api.wakeOutcome(car).kind == DONE and wake.call_count == 1
    clk.t += 50  # 150 s after the check, 50 s after the wake
    assert car.is_awake() is True and check.call_count == 1
    assert wake.last_request.timeout == 10


def test_a_refresh_spends_one_of_a_reads_three_tries(requests_mock, monkeypatch):
    # BASE counted the resend after a refresh against get_car_api's three
    # tries: a read never pays for more than three vehicle_data requests
    import TWCManager.Vehicle.TeslaAPI as T
    from tests.unit.teslatokens import FLEET_REFRESH_URL, add_home_vehicle, new_api
    from tests.unit.test_teslaapi_refresh import token_response

    monkeypatch.setattr(T.time, "sleep", Mock())
    api = new_api()
    api.storeToken(fleet_token(jti="old"), REFRESH)
    car = add_home_vehicle(api)
    refresh = requests_mock.post(
        FLEET_REFRESH_URL, json=token_response(fleet_token(jti="new"))
    )
    busy = {"json": {"response": {"reason": "could_not_wake_buses", "result": False}}}
    data = requests_mock.get(
        PATH_URLS["read"], [{"status_code": 401, "text": "token expired"}] + [busy] * 5
    )
    assert car.get_car_api(HOME_VIN + "/vehicle_data", checkReady=False) == (
        False,
        None,
    )
    assert data.call_count == 3 and refresh.call_count == 1


EXPIRED = {"status_code": 401, "text": "token expired"}
BUSY = {"json": {"response": {"reason": "could_not_wake_buses", "result": False}}}


@pytest.mark.parametrize(
    "answers, cleared",
    [
        # A refreshed token refused again on a later try retires the slot,
        # with no second refresh
        ([EXPIRED, BUSY, EXPIRED], True),
        # An expiry on the last try still refreshes, but sends nothing more
        ([BUSY, BUSY, EXPIRED], False),
    ],
)
def test_a_read_refreshes_at_most_once_across_its_tries(
    answers, cleared, requests_mock, monkeypatch
):   # slim Task 13: one tries budget in place of resend/retried
    import TWCManager.Vehicle.TeslaAPI as T
    from tests.unit.teslatokens import FLEET_REFRESH_URL, add_home_vehicle, new_api
    from tests.unit.test_teslaapi_refresh import token_response

    monkeypatch.setattr(T.time, "sleep", Mock())
    api = new_api()
    api.storeToken(fleet_token(jti="old"), REFRESH)
    car = add_home_vehicle(api)
    refresh = requests_mock.post(
        FLEET_REFRESH_URL, json=token_response(fleet_token(jti="new"))
    )
    data = requests_mock.get(PATH_URLS["read"], answers + [BUSY] * 3)
    assert car.get_car_api(HOME_VIN + "/vehicle_data", checkReady=False) == (
        False,
        None,
    )
    assert data.call_count == 3 and refresh.call_count == 1
    assert api.fleetSlot.empty() is cleared


@pytest.mark.parametrize("path", ["read", "command"])
def test_a_fleet_401_never_refreshes_the_owner_slot(path, requests_mock):
    # Both slots hold refresh tokens: only the slot that made the request
    # refreshes, at its own auth endpoint
    from tests.unit.teslatokens import (
        AUTH_TOKEN_URL,
        FLEET_REFRESH_URL,
        add_home_vehicle,
        new_api,
        owner_token,
    )
    from tests.unit.test_teslaapi_refresh import owner_refresh_token, token_response

    api = new_api()
    api.storeToken(owner_token(), owner_refresh_token())
    api.storeToken(fleet_token(jti="old"), REFRESH)
    car = add_home_vehicle(api)
    fleetAuth = requests_mock.post(
        FLEET_REFRESH_URL, json=token_response(fleet_token(jti="new"))
    )
    ownerAuth = requests_mock.post(
        AUTH_TOKEN_URL, json=token_response(owner_token(jti="new"))
    )
    requests_mock.request(
        "GET" if path == "read" else "POST",
        PATH_URLS[path],
        [
            {"status_code": 401, "text": "token expired"},
            {"json": {"response": {"result": True}}},
        ],
    )
    if path == "read":
        car.get_car_api(
            HOME_VIN + "/vehicle_data", checkReady=False, slot=api.fleetSlot
        )
    else:
        api.command(car, "charge_stop")
    assert fleetAuth.call_count == 1 and ownerAuth.call_count == 0


def test_a_list_401_whose_refresh_never_left_backs_off(requests_mock, caplog):
    # Not a refusal: the list failed, and loadVehicleList backs off and logs
    from tests.unit.teslatokens import FLEET_REFRESH_URL, new_api

    api = new_api()
    old = fleet_token(jti="old")
    api.storeToken(old, REFRESH)
    requests_mock.post(FLEET_REFRESH_URL, exc=requests.exceptions.ConnectionError)
    requests_mock.get(PATH_URLS["list"], status_code=401, text="token expired")
    caplog.set_level(1)
    assert api.loadVehicleList() is False
    assert api.fleetSlot.accessToken == old
    assert "Can't get list of vehicles" in caplog.text
