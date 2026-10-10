"""
TeslaAPI token classification.

Every accepted bearer token is classified, and the base URL follows the
current token:
  1. aud names owner-api            -> Owner API, read-only, proxy ignored
  2. Fleet (aud fleet-api / ou_code) -> teslaProxy if set, else region URL
  3. anything else                  -> unknown: baseURL "", ERROR logged
"""

import logging

import pytest
import requests_mock as rm

from tests.unit.teslatokens import (
    fleet_token,
    make_jwt,
    new_api,
    owner_token,
)

OWNER_URL = "https://owner-api.teslamotors.com/api/1/vehicles"
NA_URL = "https://fleet-api.prd.na.vn.cloud.tesla.com/api/1/vehicles"
EU_URL = "https://fleet-api.prd.eu.vn.cloud.tesla.com/api/1/vehicles"
PROXY = "https://localhost:4443"
PROXY_URL = PROXY + "/api/1/vehicles"
PROXY_CERT = "/etc/twcmanager/proxy.pem"


@pytest.fixture
def proxy_api():
    return new_api(teslaProxy=PROXY, teslaProxyCert=PROXY_CERT)


class TestOwnerToken:
    def test_live_teslamate_token_is_owner_api_read_only(self):
        api = new_api()
        assert api.setCarApiBearerToken(owner_token()) is True
        assert api.getCarApiBaseURL() == OWNER_URL
        assert api.readOnly is True

    def test_owner_token_ignores_tesla_proxy(self, proxy_api):
        proxy_api.setCarApiBearerToken(owner_token())
        assert proxy_api.getCarApiBaseURL() == OWNER_URL
        assert proxy_api.readOnly is True
        # The proxy's certificate must not be used to verify owner-api.
        assert proxy_api.verifyCert is True

    def test_aud_as_plain_string(self):
        api = new_api()
        api.setCarApiBearerToken(make_jwt(aud="https://owner-api.teslamotors.com/"))
        assert api.getCarApiBaseURL() == OWNER_URL


class TestFleetToken:
    def test_fleet_token_without_proxy_uses_region(self):
        api = new_api()
        api.setCarApiBearerToken(fleet_token())
        assert api.getCarApiBaseURL() == NA_URL
        assert api.readOnly is False
        assert api.verifyCert is True

    def test_fleet_token_with_proxy_uses_proxy(self, proxy_api):
        proxy_api.setCarApiBearerToken(fleet_token())
        assert proxy_api.getCarApiBaseURL() == PROXY_URL
        assert proxy_api.readOnly is False
        assert proxy_api.verifyCert == PROXY_CERT

    def test_ou_code_selects_region(self):
        api = new_api()
        api.setCarApiBearerToken(fleet_token(ou_code="EU"))
        assert api.getCarApiBaseURL() == EU_URL

    def test_no_ou_code_matches_aud_host(self):
        api = new_api()
        api.setCarApiBearerToken(
            make_jwt(aud=["https://fleet-api.prd.eu.vn.cloud.tesla.com"])
        )
        assert api.getCarApiBaseURL() == EU_URL

    def test_no_ou_code_unknown_fleet_host_defaults_to_na(self):
        api = new_api()
        api.setCarApiBearerToken(
            make_jwt(aud=["https://fleet-api.prd.xx.vn.cloud.tesla.com"])
        )
        assert api.getCarApiBaseURL() == NA_URL

    def test_ou_code_alone_is_fleet(self):
        api = new_api()
        api.setCarApiBearerToken(make_jwt(ou_code="NA"))
        assert api.getCarApiBaseURL() == NA_URL
        assert api.readOnly is False


class TestUnknownToken:
    @pytest.mark.parametrize(
        "token",
        ["not-a-jwt-at-all", make_jwt(sub="someone", aud=["https://example.com"])],
        ids=["undecodable", "no-recognised-claims"],
    )
    def test_unknown_token_clears_base_url_and_logs_error(
        self, proxy_api, caplog, token
    ):
        caplog.set_level(logging.INFO)
        assert proxy_api.setCarApiBearerToken(token) is True

        assert proxy_api.getCarApiBearerToken() == token  # still stored
        assert proxy_api.getCarApiBaseURL() == ""
        assert proxy_api.readOnly is False
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert errors, "expected an ERROR log line"
        assert all(token not in r.getMessage() for r in caplog.records)

    def test_unknown_token_makes_no_api_calls(self, requests_mock, monkeypatch):
        import TWCManager.Vehicle.TeslaAPI as teslaapi

        monkeypatch.setattr(teslaapi, "Thread", lambda *a, **k: _NoThread())
        any_call = requests_mock.register_uri(rm.ANY, rm.ANY, json={})
        api = new_api()
        api.setCarApiBearerToken(fleet_token())
        api.addVehicle({"id": 1, "vin": "VIN1", "display_name": "Car"})
        vehicle = api.getCarApiVehicles()[0]
        api.setCarApiBearerToken("not-a-jwt-at-all")

        assert api.car_api_available() is False
        assert vehicle.is_awake() is False
        assert api.wakeVehicle(vehicle) is False
        assert api.setChargeRate(10, vehicle) is False

        assert any_call.call_count == 0
        assert api.getCarApiLastErrorTime() == 0
        assert vehicle.lastErrorTime == 0


class _NoThread:
    daemon = False

    def start(self):
        pass


class TestSwitching:
    def test_owner_fleet_owner_restores_url_each_time(self, proxy_api):
        proxy_api.setCarApiBearerToken(owner_token())
        assert (proxy_api.getCarApiBaseURL(), proxy_api.readOnly) == (OWNER_URL, True)

        proxy_api.setCarApiBearerToken(fleet_token())
        assert (proxy_api.getCarApiBaseURL(), proxy_api.readOnly) == (
            PROXY_URL,
            False,
        )
        assert proxy_api.verifyCert == PROXY_CERT

        proxy_api.setCarApiBearerToken(owner_token())
        assert (proxy_api.getCarApiBaseURL(), proxy_api.readOnly) == (OWNER_URL, True)
        assert proxy_api.verifyCert is True

    def test_switch_without_proxy(self):
        api = new_api()
        for token, url in [
            (owner_token(), OWNER_URL),
            (fleet_token(ou_code="EU"), EU_URL),
            (owner_token(), OWNER_URL),
        ]:
            api.setCarApiBearerToken(token)
            assert api.getCarApiBaseURL() == url

    def test_vehicle_verify_follows_token(self, proxy_api, monkeypatch):
        import TWCManager.Vehicle.TeslaAPI as teslaapi

        monkeypatch.setattr(teslaapi, "Thread", lambda *a, **k: _NoThread())
        proxy_api.setCarApiBearerToken(fleet_token())
        proxy_api.addVehicle({"id": 1, "vin": "VIN1", "display_name": "Car"})
        vehicle = proxy_api.getCarApiVehicles()[0]
        assert vehicle.verifyCert == PROXY_CERT

        proxy_api.setCarApiBearerToken(owner_token())
        assert vehicle.verifyCert is True

    def test_classification_logged_without_token(self, caplog):
        caplog.set_level(logging.INFO)
        api = new_api()
        token = owner_token()
        api.setCarApiBearerToken(token)
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert "owner-api.teslamotors.com" in text
        assert "read-only" in text
        assert token not in text


class TestRegionListsExcludeOwnerApi:
    def test_login_regions_are_fleet_only(self):
        api = new_api(teslaApiRedirectUri="https://twc.example.com/cb")
        assert "OwnerAPI" in api.regionURL
        assert api.teslaLoginInfo()["regions"] == ["NA", "EU", "CN"]

    def test_login_url_rejects_owner_api_region(self):
        api = new_api(
            teslaApiRedirectUri="https://twc.example.com/cb",
            teslaApiClientSecret="secret",
            teslaApiRegion="EU",
        )
        url = api.getLoginURL("OwnerAPI")
        assert "fleet-api.prd.eu.vn.cloud.tesla.com" in url
        assert "owner-api" not in url

    def test_region_audience_never_owner_api(self):
        api = new_api()
        assert api.regionAudience("OwnerAPI") == (
            "https://fleet-api.prd.na.vn.cloud.tesla.com"
        )
