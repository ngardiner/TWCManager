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

VIN = "5YJ3E1EA0KF000001"
# Any Tesla token endpoint; which host refreshes a token varies by release.
TOKEN_URL = re.compile(r"/oauth2/v3/token$")


def make_master():
    master = Mock()
    master.config = {
        "config": {"teslaApiClientID": "my-client-id"},
        "vehicle": {"teslaAPI": {"enabled": True}},
    }
    # tokenSyncEnabled() must be falsey or setCarApiBearerToken refuses the token
    master.tokenSyncEnabled.return_value = False
    return master


@pytest.fixture
def api():
    from TWCManager.Vehicle.TeslaAPI import TeslaAPI

    api = TeslaAPI(make_master())
    # Start each test without inherited class-level token state
    api.carApiBearerToken = "old-access"
    api.carApiRefreshToken = "old-refresh"
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

        assert api.apiRefresh() is False

        # No 401 came back, so the tokens are kept for a later retry.
        assert api.getCarApiRefreshToken() == "old-refresh"
        assert api.getCarApiLastErrorTime() > 0

    def test_401_still_clears_tokens(self, api, requests_mock):
        requests_mock.post(TOKEN_URL, status_code=401, json={})

        assert api.apiRefresh() is False
        assert api.getCarApiRefreshToken() == ""


class TestWake:
    def test_request_never_sent(self, api, vehicle, requests_mock):
        requests_mock.post(
            api.baseURL + "/" + VIN + "/wake_up",
            exc=requests.exceptions.ConnectionError,
        )
        refresh = requests_mock.post(TOKEN_URL, json={})

        assert api.wakeVehicle(vehicle) is False
        assert refresh.called is False


class TestGetCarApi:
    URL = "https://vehicle.example/api/1/vehicles/" + VIN + "/vehicle_data"

    def test_request_never_sent(self, vehicle, requests_mock):
        requests_mock.get(self.URL, exc=requests.exceptions.ConnectionError)

        assert vehicle.get_car_api(self.URL, checkReady=False) == (False, None)

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
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "expires_in": 28800,
            },
        )

        ok, response = vehicle.get_car_api(self.URL, checkReady=False)

        assert ok is True
        assert response == {"ok": True}
        assert data.call_count == 2
        assert data.request_history[1].headers["Authorization"] == "Bearer new-access"
