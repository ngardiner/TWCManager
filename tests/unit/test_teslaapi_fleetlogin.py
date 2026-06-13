"""
Unit tests for the TeslaAPI FleetAPI OAuth login flow.

Covers authorization URL construction, login state validation and the
authorization-code token exchange (including the paste-back path).
"""

from urllib.parse import urlsplit, parse_qs

import pytest
from unittest.mock import Mock

TOKEN_URL = "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token"


def make_master(**config_overrides):
    """Mock master whose .config carries the FleetAPI login settings."""
    cfg = {
        "teslaApiClientID": "my-client-id",
        "teslaApiClientSecret": "my-client-secret",
        "teslaApiRedirectUri": "https://twc.example.com/teslaAccount/callback",
        "teslaApiRegion": "NA",
    }
    cfg.update(config_overrides)

    master = Mock()
    master.config = {"config": cfg, "vehicle": {"teslaAPI": {"enabled": True}}}
    # tokenSyncEnabled() must be falsey or setCarApiBearerToken refuses the token
    master.tokenSyncEnabled.return_value = False
    return master


@pytest.fixture
def teslaapi():
    from TWCManager.Vehicle.TeslaAPI import TeslaAPI

    api = TeslaAPI(make_master())
    # Start each test without inherited class-level token state
    api.carApiBearerToken = ""
    api.carApiRefreshToken = ""
    return api


class TestLoginURL:
    def test_login_url_built_correctly(self, teslaapi):
        url = teslaapi.getLoginURL("NA")
        split = urlsplit(url)
        assert split.scheme == "https"
        assert split.netloc == "auth.tesla.com"
        assert split.path == "/oauth2/v3/authorize"

        qs = parse_qs(split.query)
        assert qs["response_type"] == ["code"]
        assert qs["client_id"] == ["my-client-id"]
        assert qs["redirect_uri"] == ["https://twc.example.com/teslaAccount/callback"]
        assert qs["audience"] == ["https://fleet-api.prd.na.vn.cloud.tesla.com"]
        assert "vehicle_charging_cmds" in qs["scope"][0]
        assert qs["state"][0]  # a state value was generated
        # State is remembered for the later exchange
        assert teslaapi._TeslaAPI__loginState == qs["state"][0]

    def test_login_url_region_audience(self, teslaapi):
        qs = parse_qs(urlsplit(teslaapi.getLoginURL("EU")).query)
        assert qs["audience"] == ["https://fleet-api.prd.eu.vn.cloud.tesla.com"]

    def test_login_url_empty_when_not_configured(self):
        from TWCManager.Vehicle.TeslaAPI import TeslaAPI

        api = TeslaAPI(make_master(teslaApiClientSecret=""))
        assert api.getLoginURL("NA") == ""
        assert api.teslaLoginInfo()["configured"] is False


class TestTokenExchange:
    def test_state_mismatch_rejected(self, teslaapi, requests_mock):
        adapter = requests_mock.post(TOKEN_URL, json={})
        teslaapi.getLoginURL("NA")  # sets a known state
        result = teslaapi.fleetTokenExchange("auth-code", "not-the-state")
        assert result == "state_mismatch"
        assert adapter.call_count == 0
        assert teslaapi.getCarApiBearerToken() == ""

    def test_successful_exchange_stores_tokens(self, teslaapi, requests_mock):
        requests_mock.post(
            TOKEN_URL,
            json={
                "access_token": "ACCESS123",
                "refresh_token": "REFRESH456",
                "expires_in": 28800,
            },
        )
        url = teslaapi.getLoginURL("NA")
        state = parse_qs(urlsplit(url).query)["state"][0]

        result = teslaapi.fleetTokenExchange("auth-code", state)

        assert result == "success"
        assert teslaapi.getCarApiBearerToken() == "ACCESS123"
        assert teslaapi.getCarApiRefreshToken() == "REFRESH456"

        # The exchange must send the confidential-client fields Tesla requires
        body = parse_qs(requests_mock.last_request.text)
        assert body["grant_type"] == ["authorization_code"]
        assert body["client_id"] == ["my-client-id"]
        assert body["client_secret"] == ["my-client-secret"]
        assert body["code"] == ["auth-code"]
        assert body["audience"] == ["https://fleet-api.prd.na.vn.cloud.tesla.com"]
        assert body["redirect_uri"] == ["https://twc.example.com/teslaAccount/callback"]
        # State is consumed after a successful exchange
        assert teslaapi._TeslaAPI__loginState is None

    def test_tesla_error_returned(self, teslaapi, requests_mock):
        requests_mock.post(TOKEN_URL, json={"error": "invalid_grant"})
        state = parse_qs(urlsplit(teslaapi.getLoginURL("NA")).query)["state"][0]
        assert teslaapi.fleetTokenExchange("auth-code", state) == "invalid_grant"
        assert teslaapi.getCarApiBearerToken() == ""

    def test_not_configured_returns_status(self, requests_mock):
        from TWCManager.Vehicle.TeslaAPI import TeslaAPI

        api = TeslaAPI(make_master(teslaApiClientSecret=""))
        assert api.fleetTokenExchange("auth-code", "state") == "not_configured"

    def test_paste_back_path(self, teslaapi, requests_mock):
        requests_mock.post(
            TOKEN_URL,
            json={
                "access_token": "ACCESS123",
                "refresh_token": "REFRESH456",
                "expires_in": 28800,
            },
        )
        state = parse_qs(urlsplit(teslaapi.getLoginURL("NA")).query)["state"][0]
        pasted = (
            "https://twc.example.com/teslaAccount/callback"
            "?code=pasted-code&state=" + state
        )
        assert teslaapi.saveApiToken(pasted) == "success"
        assert teslaapi.getCarApiBearerToken() == "ACCESS123"
        assert parse_qs(requests_mock.last_request.text)["code"] == ["pasted-code"]
