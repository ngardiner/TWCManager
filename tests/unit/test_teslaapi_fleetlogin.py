"""
Unit tests for the TeslaAPI FleetAPI Authorization Code login flow.

Two variants are auto-selected by whether a client secret is configured:
  - PKCE / public client (default, no secret): code_challenge, no audience,
    tokens exchanged at auth.tesla.com.
  - Confidential client (secret set): client_secret + audience, tokens
    exchanged at the regional fleet-auth endpoint.
"""

from urllib.parse import urlsplit, parse_qs

import pytest
from unittest.mock import Mock

PKCE_TOKEN_URL = "https://auth.tesla.com/oauth2/v3/token"
CONF_TOKEN_URL = "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token"
REDIRECT = "https://twc.example.com/teslaAccount/callback"


def make_master(**config_overrides):
    """Mock master whose .config carries the FleetAPI login settings.

    Defaults to the PKCE flow (no client secret); pass teslaApiClientSecret to
    exercise the confidential flow.
    """
    cfg = {
        "teslaApiClientID": "my-client-id",
        "teslaApiRedirectUri": REDIRECT,
    }
    cfg.update(config_overrides)

    master = Mock()
    master.config = {"config": cfg, "vehicle": {"teslaAPI": {"enabled": True}}}
    # tokenSyncEnabled() must be falsey or setCarApiBearerToken refuses the token
    master.tokenSyncEnabled.return_value = False
    return master


def new_api(**config_overrides):
    from TWCManager.Vehicle.TeslaAPI import TeslaAPI

    api = TeslaAPI(make_master(**config_overrides))
    # Start each test without inherited class-level token state
    api.carApiBearerToken = ""
    api.carApiRefreshToken = ""
    return api


@pytest.fixture
def pkce_api():
    return new_api()  # no secret -> PKCE


@pytest.fixture
def conf_api():
    return new_api(teslaApiClientSecret="my-client-secret", teslaApiRegion="NA")


class TestMode:
    def test_pkce_is_default(self, pkce_api):
        assert pkce_api.usePKCE() is True
        info = pkce_api.teslaLoginInfo()
        assert info["configured"] is True and info["pkce"] is True

    def test_secret_selects_confidential(self, conf_api):
        assert conf_api.usePKCE() is False
        assert conf_api.teslaLoginInfo()["pkce"] is False

    def test_not_configured_without_client_id(self):
        api = new_api(teslaApiClientID="")
        assert api.getLoginURL("NA") == ""
        assert api.teslaLoginInfo()["configured"] is False
        assert api.fleetTokenExchange("auth-code", "state") == "not_configured"


class TestLoginURL:
    def test_pkce_url_has_challenge_no_audience(self, pkce_api):
        qs = parse_qs(urlsplit(pkce_api.getLoginURL("NA")).query)
        assert qs["response_type"] == ["code"]
        assert qs["client_id"] == ["my-client-id"]
        assert qs["redirect_uri"] == [REDIRECT]
        assert qs["code_challenge_method"] == ["S256"]
        assert qs["code_challenge"][0]
        assert "audience" not in qs
        # location scope is needed for home geofencing
        assert "vehicle_location" in qs["scope"][0]
        assert "vehicle_charging_cmds" in qs["scope"][0]
        assert pkce_api._TeslaAPI__loginState == qs["state"][0]

    def test_confidential_url_has_audience_no_challenge(self, conf_api):
        qs = parse_qs(urlsplit(conf_api.getLoginURL("EU")).query)
        assert qs["audience"] == ["https://fleet-api.prd.eu.vn.cloud.tesla.com"]
        assert "code_challenge" not in qs


class TestTokenExchange:
    def test_state_mismatch_rejected(self, pkce_api, requests_mock):
        adapter = requests_mock.post(PKCE_TOKEN_URL, json={})
        pkce_api.getLoginURL("NA")
        assert pkce_api.fleetTokenExchange("auth-code", "wrong") == "state_mismatch"
        assert adapter.call_count == 0
        assert pkce_api.getCarApiBearerToken() == ""

    def test_pkce_exchange_uses_verifier_no_secret(self, pkce_api, requests_mock):
        requests_mock.post(
            PKCE_TOKEN_URL,
            json={"access_token": "A", "refresh_token": "R", "expires_in": 28800},
        )
        state = parse_qs(urlsplit(pkce_api.getLoginURL("NA")).query)["state"][0]
        assert pkce_api.fleetTokenExchange("auth-code", state) == "success"
        assert pkce_api.getCarApiBearerToken() == "A"
        assert pkce_api.getCarApiRefreshToken() == "R"

        body = parse_qs(requests_mock.last_request.text)
        assert body["grant_type"] == ["authorization_code"]
        assert body["client_id"] == ["my-client-id"]
        assert body["code"] == ["auth-code"]
        assert body["redirect_uri"] == [REDIRECT]
        assert body["code_verifier"][0]
        assert "client_secret" not in body
        assert "audience" not in body
        # state + verifier consumed
        assert pkce_api._TeslaAPI__loginState is None
        assert pkce_api._TeslaAPI__loginVerifier is None

    def test_confidential_exchange_uses_secret_audience(self, conf_api, requests_mock):
        requests_mock.post(
            CONF_TOKEN_URL,
            json={"access_token": "A", "refresh_token": "R", "expires_in": 28800},
        )
        state = parse_qs(urlsplit(conf_api.getLoginURL("NA")).query)["state"][0]
        assert conf_api.fleetTokenExchange("auth-code", state) == "success"

        body = parse_qs(requests_mock.last_request.text)
        assert body["client_secret"] == ["my-client-secret"]
        assert body["audience"] == ["https://fleet-api.prd.na.vn.cloud.tesla.com"]
        assert "code_verifier" not in body

    def test_tesla_error_returned(self, pkce_api, requests_mock):
        requests_mock.post(PKCE_TOKEN_URL, json={"error": "invalid_grant"})
        state = parse_qs(urlsplit(pkce_api.getLoginURL("NA")).query)["state"][0]
        assert pkce_api.fleetTokenExchange("auth-code", state) == "invalid_grant"
        assert pkce_api.getCarApiBearerToken() == ""

    def test_paste_back_path(self, pkce_api, requests_mock):
        requests_mock.post(
            PKCE_TOKEN_URL,
            json={"access_token": "A", "refresh_token": "R", "expires_in": 28800},
        )
        state = parse_qs(urlsplit(pkce_api.getLoginURL("NA")).query)["state"][0]
        pasted = f"{REDIRECT}?code=pasted-code&state={state}"
        assert pkce_api.saveApiToken(pasted) == "success"
        assert parse_qs(requests_mock.last_request.text)["code"] == ["pasted-code"]
