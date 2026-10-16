"""
TeslaAPI token classification.

Every stored access token is classified, which picks its slot and the
slot's base URL:
  1. aud names owner-api            -> Owner slot, Owner API, proxy ignored
  2. Fleet (aud fleet-api / ou_code) -> Fleet slot, teslaProxy if set, else
                                       region URL
  3. anything else                  -> unknown: not stored, ERROR logged
"""

import logging

import pytest
import requests_mock as rm

from tests.unit.teslatokens import (
    add_home_vehicle,
    fleet_token,
    make_jwt,
    new_api,
    no_threads,
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
        assert api.storeToken(owner_token()) == "success"
        assert api.ownerSlot.baseURL == OWNER_URL
        assert api.slotFor("command") is None

    def test_owner_token_ignores_tesla_proxy(self, proxy_api):
        proxy_api.storeToken(owner_token())
        assert proxy_api.ownerSlot.baseURL == OWNER_URL
        assert proxy_api.slotFor("command") is None
        # The proxy's certificate must not be used to verify owner-api.
        assert proxy_api.ownerSlot.verifyCert is True

    def test_aud_as_plain_string(self):
        api = new_api()
        api.storeToken(make_jwt(aud="https://owner-api.teslamotors.com/"))
        assert api.ownerSlot.baseURL == OWNER_URL


class TestFleetToken:
    def test_fleet_token_without_proxy_uses_region(self):
        api = new_api()
        api.storeToken(fleet_token())
        assert api.fleetSlot.baseURL == NA_URL
        assert api.readOnly is False
        assert api.fleetSlot.verifyCert is True

    def test_fleet_token_with_proxy_uses_proxy(self, proxy_api):
        proxy_api.storeToken(fleet_token())
        assert proxy_api.fleetSlot.baseURL == PROXY_URL
        assert proxy_api.readOnly is False
        assert proxy_api.fleetSlot.verifyCert == PROXY_CERT

    def test_ou_code_selects_region(self):
        api = new_api()
        api.storeToken(fleet_token(ou_code="EU"))
        assert api.fleetSlot.baseURL == EU_URL

    def test_no_ou_code_matches_aud_host(self):
        api = new_api()
        api.storeToken(make_jwt(aud=["https://fleet-api.prd.eu.vn.cloud.tesla.com"]))
        assert api.fleetSlot.baseURL == EU_URL

    def test_no_ou_code_unknown_fleet_host_defaults_to_na(self):
        api = new_api()
        api.storeToken(make_jwt(aud=["https://fleet-api.prd.xx.vn.cloud.tesla.com"]))
        assert api.fleetSlot.baseURL == NA_URL

    def test_ou_code_alone_is_fleet(self):
        api = new_api()
        api.storeToken(make_jwt(ou_code="NA"))
        assert api.fleetSlot.baseURL == NA_URL
        assert api.readOnly is False


def nothing_stored(api):
    return api.ownerSlot.accessToken == "" and api.fleetSlot.accessToken == ""


class TestUnknownToken:
    @pytest.mark.parametrize(
        "token",
        ["not-a-jwt-at-all", make_jwt(sub="someone", aud=["https://example.com"])],
        ids=["undecodable", "no-recognised-claims"],
    )
    def test_unknown_token_is_refused_and_logs_error(self, proxy_api, caplog, token):
        caplog.set_level(logging.INFO)
        assert proxy_api.storeToken(token) == "unknown_type"

        assert nothing_stored(proxy_api)
        assert proxy_api.readOnly is True
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert errors, "expected an ERROR log line"
        assert all(token not in r.getMessage() for r in caplog.records)

    def test_unknown_token_makes_no_api_calls(self, requests_mock, no_threads):
        any_call = requests_mock.register_uri(rm.ANY, rm.ANY, json={})
        api = new_api()
        api.storeToken(fleet_token())
        vehicle = add_home_vehicle(api)
        vehicle.lastAPIAccessTime = 0
        # As restored from settings: a token of unknown type in the slot.
        api.fleetSlot.setTokens("not-a-jwt-at-all")

        assert api.car_api_available() is False
        assert vehicle.is_awake() is False
        assert api.wakeVehicle(vehicle) is False
        assert api.setChargeRate(10, vehicle) is False

        assert any_call.call_count == 0
        assert api.getCarApiLastErrorTime() == 0
        assert vehicle.lastErrorTime == 0


class TestNoBaseURLLogLine:
    """car_api_available stops when no slot is usable. Its log line must
    tell "no token yet" apart from "token type unknown"."""

    def messages(self, api, caplog, requests_mock):
        any_call = requests_mock.register_uri(rm.ANY, rm.ANY, json={})
        caplog.set_level(logging.INFO8)
        assert api.car_api_available() is False
        assert any_call.call_count == 0
        return [r.getMessage() for r in caplog.records]

    def test_no_token_yet(self, caplog, requests_mock):
        api = new_api()
        messages = self.messages(api, caplog, requests_mock)
        assert any("no Tesla API token yet" in m for m in messages)
        assert not any("token type unknown" in m for m in messages)

    def test_unknown_token(self, caplog, requests_mock):
        api = new_api()
        # As restored from settings: a token of unknown type in the slot.
        api.restoreSlot(api.fleetSlot, "not-a-jwt-at-all", "", None)
        messages = self.messages(api, caplog, requests_mock)
        assert any("token type unknown" in m for m in messages)
        assert not any("no Tesla API token yet" in m for m in messages)


class TestOddAud:
    """aud is normally a string or a list; other shapes must not raise out of
    storeToken."""

    def test_null_aud_falls_through_to_ou_code(self):
        api = new_api()
        assert api.storeToken(make_jwt(aud=None, ou_code="EU")) == "success"
        assert api.fleetSlot.baseURL == EU_URL
        assert api.readOnly is False

    def test_null_aud_without_ou_code_is_unknown(self):
        api = new_api()
        assert api.storeToken(make_jwt(aud=None)) == "unknown_type"
        assert nothing_stored(api)

    @pytest.mark.parametrize(
        "aud",
        [42, {"host": "https://owner-api.teslamotors.com/"}, True],
        ids=["number", "object", "boolean"],
    )
    def test_other_aud_types_are_unknown(self, aud):
        api = new_api()
        token = make_jwt(aud=aud, ou_code="NA")
        assert api.storeToken(token) == "unknown_type"
        assert nothing_stored(api)
        assert api.readOnly is True


class TestSwitching:
    """Each slot keeps its own token's base URL and certificate, whatever
    the other slot holds."""

    def test_owner_and_fleet_keep_their_own_routes(self, proxy_api):
        proxy_api.storeToken(owner_token())
        assert proxy_api.ownerSlot.baseURL == OWNER_URL
        assert proxy_api.slotFor("command") is None

        proxy_api.storeToken(fleet_token())
        assert proxy_api.fleetSlot.baseURL == PROXY_URL
        assert proxy_api.fleetSlot.verifyCert == PROXY_CERT
        assert proxy_api.readOnly is False

        proxy_api.storeToken(owner_token())
        assert proxy_api.ownerSlot.baseURL == OWNER_URL
        assert proxy_api.ownerSlot.verifyCert is True
        assert proxy_api.fleetSlot.baseURL == PROXY_URL
        assert proxy_api.fleetSlot.verifyCert == PROXY_CERT

    def test_switch_without_proxy(self):
        api = new_api()
        for token, slot, url in [
            (owner_token(), api.ownerSlot, OWNER_URL),
            (fleet_token(ou_code="EU"), api.fleetSlot, EU_URL),
            (owner_token(), api.ownerSlot, OWNER_URL),
        ]:
            api.storeToken(token)
            assert slot.baseURL == url

    def test_vehicle_verify_follows_token(self, proxy_api, no_threads, requests_mock):
        proxy = requests_mock.get(PROXY_URL + "/VIN1", json={"response": {}})
        owner = requests_mock.get(OWNER_URL + "/VIN1", json={"response": {}})
        proxy_api.storeToken(fleet_token())
        vehicle = add_home_vehicle(proxy_api)
        vehicle.is_awake()
        assert proxy.last_request.verify == PROXY_CERT

        proxy_api.storeToken(owner_token())
        vehicle.lastVehicleAwakeTime = 0
        vehicle.is_awake()
        assert owner.last_request.verify is True

    def test_classification_logged_without_token(self, caplog):
        caplog.set_level(logging.INFO)
        api = new_api()
        token = owner_token()
        api.storeToken(token)
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert "owner-api.teslamotors.com" in text
        assert "token type: owner" in text
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
