"""
Tesla API token refresh. A hand-pasted Owner API token refreshes the way
release/1.3.3 refreshes it (client_id "ownerapi" at auth.tesla.com, over the
TLS 1.3 session); every other token keeps the Fleet refresh.
"""

import json
import ssl
import time
from unittest.mock import Mock, patch

import pytest
import requests

from tests.unit import teslatokens
from tests.unit.teslatokens import (
    AUTH_TOKEN_URL,
    FLEET_REFRESH_URL,
    fleet_token,
    make_jwt,
    owner_token,
)

OWNER_ISSUER = "https://auth.tesla.com/oauth2/v3"
OWNER_API_URL = "https://owner-api.teslamotors.com/api/1/vehicles"
OPAQUE_REFRESH = "NA_0123456789abcdef0123456789abcdef"


def new_api(**config_overrides):
    """A TeslaAPI for a confidential Fleet client. 1.3.4 refreshes a
    public-client (PKCE) Fleet token at auth.tesla.com, so the client secret
    keeps the Fleet refresh at fleet-auth, apart from the Owner refresh."""
    config_overrides.setdefault("teslaApiClientSecret", "my-client-secret")
    return teslatokens.new_api(**config_overrides)


def owner_refresh_token(**extra):
    """An Owner API refresh token as TeslaMate holds it: a JWT issued by
    auth.tesla.com/oauth2/v3 with the token endpoint as aud, ~90 days."""
    claims = {
        "iss": OWNER_ISSUER,
        "aud": "https://auth.tesla.com/oauth2/v3/token",
        "exp": int(time.time() + 90 * 24 * 60 * 60),
    }
    claims.update(extra)
    return make_jwt(**claims)


def token_response(access):
    return {
        "access_token": access,
        "refresh_token": owner_refresh_token(),
        "expires_in": 28800,
    }


def sync_on(api):
    """Register a TeslaMate stand-in with syncTokens on, which is what makes
    FakeMaster.tokenSyncEnabled() (like TWCMaster's) return True."""

    class TeslaMateStandIn:
        syncTokens = True

    api.master.modules["TeslaMateVehicle"] = TeslaMateStandIn()


class TestOwnerRefreshRequest:
    def test_owner_refresh_matches_release_1_3_3(self, requests_mock):
        # The request below is transcribed from release/1.3.3's apiRefresh
        # (lib/TWCManager/Vehicle/TeslaAPI.py:156-174 @ 1.3.3): refreshURL
        # auth.tesla.com/oauth2/v3/token, refreshClientID "ownerapi", sent
        # as JSON through self.session.
        owner = requests_mock.post(
            "https://auth.tesla.com/oauth2/v3/token",
            json=token_response(owner_token()),
        )
        fleet = requests_mock.post(FLEET_REFRESH_URL, json={})
        api = new_api()
        api.setCarApiBearerToken(owner_token(expires_in=60))
        api.carApiRefreshToken = "owner-refresh"

        with patch.object(api.session, "post", wraps=api.session.post) as post:
            assert api.apiRefresh() is True
        assert post.call_count == 1

        assert owner.call_count == 1
        assert fleet.call_count == 0
        sent = owner.last_request
        assert sent.url == "https://auth.tesla.com/oauth2/v3/token"
        assert sent.headers["accept"] == "application/json"
        assert sent.headers["Content-Type"] == "application/json"
        assert sent.json() == {
            "client_id": "ownerapi",
            "grant_type": "refresh_token",
            "refresh_token": "owner-refresh",
            "scope": "offline_access",
        }

    def test_session_requires_tls_1_3_at_auth_tesla_com(self):
        # release/1.3.3 mounts TLS13Adapter on https://auth.tesla.com
        # (TeslaAPI.py:20-31, 113-122 @ 1.3.3).
        from TWCManager.Vehicle.TeslaAPI import TLS13Adapter

        api = new_api()
        adapter = api.session.get_adapter("https://auth.tesla.com/oauth2/v3/token")
        assert isinstance(adapter, TLS13Adapter)
        context = adapter.poolmanager.connection_pool_kw["ssl_context"]
        assert context.minimum_version == ssl.TLSVersion.TLSv1_3


class TestFleetRefreshUnchanged:
    def test_fleet_refresh_request(self, requests_mock):
        fleet = requests_mock.post(
            FLEET_REFRESH_URL, json=token_response(fleet_token())
        )
        owner = requests_mock.post(AUTH_TOKEN_URL, json={})
        api = new_api()
        api.setCarApiBearerToken(fleet_token(expires_in=60))
        api.carApiRefreshToken = OPAQUE_REFRESH

        with patch.object(api.session, "post") as post:
            assert api.apiRefresh() is True
        assert post.call_count == 0

        assert owner.call_count == 0
        assert fleet.call_count == 1
        sent = fleet.last_request
        assert sent.headers["accept"] == "application/json"
        assert sent.headers["Content-Type"] == "application/json"
        assert sent.json() == {
            "client_id": "my-client-id",
            "grant_type": "refresh_token",
            "refresh_token": OPAQUE_REFRESH,
            "scope": "offline_access",
        }

    def test_public_client_fleet_refresh_request(self, requests_mock):
        # Without a client secret, 1.3.4 refreshes a Fleet token at
        # auth.tesla.com too; only client_id and the session tell the two
        # refreshes apart.
        refresh = requests_mock.post(AUTH_TOKEN_URL, json=token_response(fleet_token()))
        fleet = requests_mock.post(FLEET_REFRESH_URL, json={})
        api = new_api(teslaApiClientSecret="")
        api.setCarApiBearerToken(fleet_token(expires_in=60))
        api.carApiRefreshToken = OPAQUE_REFRESH

        with patch.object(api.session, "post") as post:
            assert api.apiRefresh() is True
        assert post.call_count == 0

        assert fleet.call_count == 0
        assert refresh.call_count == 1
        assert refresh.last_request.json() == {
            "client_id": "my-client-id",
            "grant_type": "refresh_token",
            "refresh_token": OPAQUE_REFRESH,
            "scope": "offline_access",
        }

    def test_wake_401_refreshes_a_fleet_token_at_fleet_auth(self, requests_mock):
        # wakeVehicle refuses before any request for an Owner token, so its
        # 401 refresh only ever sees Fleet tokens.
        fleet = requests_mock.post(
            FLEET_REFRESH_URL, json=token_response(fleet_token())
        )
        owner = requests_mock.post(AUTH_TOKEN_URL, json={})
        api = new_api()
        api.setCarApiBearerToken(fleet_token())
        api.carApiRefreshToken = OPAQUE_REFRESH
        wake = api.baseURL + "/VIN1/wake_up"
        requests_mock.post(
            wake,
            [
                {"status_code": 401, "text": "token expired"},
                {"json": {"response": {"state": "online"}}},
            ],
        )

        class Vehicle:
            VIN = "VIN1"

        assert api.wakeVehicle(Vehicle()) == {"response": {"state": "online"}}
        assert fleet.call_count == 1
        assert owner.call_count == 0


class TestPastedRefreshToken:
    def test_owner_refresh_token_alone_gets_owner_refresh(self, requests_mock):
        new_access = owner_token()
        owner = requests_mock.post(AUTH_TOKEN_URL, json=token_response(new_access))
        fleet = requests_mock.post(FLEET_REFRESH_URL, json={})
        api = new_api()
        pasted = owner_refresh_token()

        assert api.setCarApiRefreshToken(pasted) is True

        assert fleet.call_count == 0
        assert owner.call_count == 1
        assert owner.last_request.json()["client_id"] == "ownerapi"
        assert owner.last_request.json()["refresh_token"] == pasted
        assert api.getCarApiBearerToken() == new_access
        assert api.readOnly is True
        assert api.getCarApiBaseURL() == OWNER_API_URL

    def test_owner_refresh_token_pasted_back(self, requests_mock):
        # The /teslaAccount/saveToken form takes a bare token as well as a
        # redirect URL.
        new_access = owner_token()
        owner = requests_mock.post(AUTH_TOKEN_URL, json=token_response(new_access))
        fleet = requests_mock.post(FLEET_REFRESH_URL, json={})
        api = new_api()

        assert api.saveApiToken(owner_refresh_token()) == "success"

        assert fleet.call_count == 0
        assert owner.call_count == 1
        assert api.getCarApiBearerToken() == new_access
        assert api.readOnly is True

    def test_owner_access_token_pasted_back(self, requests_mock):
        refresh = requests_mock.post(AUTH_TOKEN_URL, json={})
        api = new_api()
        access = owner_token()

        assert api.saveApiToken(access) == "success"

        assert refresh.call_count == 0
        assert api.getCarApiBearerToken() == access
        assert api.readOnly is True
        assert {"cmd": "saveSettings"} in api.master.tasks

    @pytest.mark.parametrize(
        "pasted",
        ["https://example.com/teslaAccount/callback", "https://example.com/cb?x=1"],
    )
    def test_url_without_code_is_not_stored(self, requests_mock, pasted):
        refresh = requests_mock.post(AUTH_TOKEN_URL, json={})
        fleet = requests_mock.post(FLEET_REFRESH_URL, json={})
        api = new_api()
        api.carApiRefreshToken = "kept-refresh"

        assert api.saveApiToken(pasted) != "success"

        assert api.getCarApiRefreshToken() == "kept-refresh"
        assert refresh.called is False
        assert fleet.called is False

    def test_opaque_refresh_token_gets_fleet_refresh(self, requests_mock):
        fleet = requests_mock.post(
            FLEET_REFRESH_URL, json=token_response(fleet_token())
        )
        owner = requests_mock.post(AUTH_TOKEN_URL, json={})
        api = new_api()
        # As left behind by an earlier Owner token that a 401 wiped:
        # setCarApiBearerToken("") does not reclassify.
        api.readOnly = True

        assert api.setCarApiRefreshToken(OPAQUE_REFRESH) is True

        assert owner.call_count == 0
        assert fleet.call_count == 1
        assert fleet.last_request.json()["client_id"] == "my-client-id"
        assert api.readOnly is False

    def test_fleet_issuer_is_not_owner(self, requests_mock):
        # A Fleet token's iss (.../oauth2/v3/nts) starts with the Owner
        # issuer; only an exact match is an Owner refresh token.
        fleet = requests_mock.post(
            FLEET_REFRESH_URL, json=token_response(fleet_token())
        )
        owner = requests_mock.post(AUTH_TOKEN_URL, json={})
        api = new_api()

        api.setCarApiRefreshToken(owner_refresh_token(iss=OWNER_ISSUER + "/nts"))

        assert owner.call_count == 0
        assert fleet.call_count == 1

    def test_refresh_if_needed_uses_owner_refresh(self, requests_mock):
        owner = requests_mock.post(AUTH_TOKEN_URL, json=token_response(owner_token()))
        fleet = requests_mock.post(FLEET_REFRESH_URL, json={})
        api = new_api()
        api.setCarApiBearerToken(owner_token(expires_in=60))
        api.carApiRefreshToken = owner_refresh_token()

        api.refreshTokenIfNeeded()

        assert fleet.call_count == 0
        assert owner.call_count == 1


class TestRefreshTokenPastedAlone:
    """A refresh token pasted on its own replaces the stored bearer token,
    which may be of the other type, and refreshes by its own type."""

    def save_settings(self, api, fields):
        from TWCManager.Control.HTTPControl import CreateHTTPHandlerClass

        handler = CreateHTTPHandlerClass(api.master)
        request = Mock()
        request.fields = fields
        request.getFieldValue = fields.get
        handler.process_save_settings(request)
        return request

    def test_owner_refresh_over_stale_fleet_bearer(self, requests_mock):
        new_access = owner_token()
        owner = requests_mock.post(AUTH_TOKEN_URL, json=token_response(new_access))
        fleet = requests_mock.post(FLEET_REFRESH_URL, json={})
        api = new_api()
        api.setCarApiBearerToken(fleet_token(expires_in=-60))
        pasted = owner_refresh_token()

        self.save_settings(api, {"carApiRefreshToken": pasted})

        assert fleet.call_count == 0
        assert owner.call_count == 1
        assert owner.last_request.json()["refresh_token"] == pasted
        assert api.getCarApiBearerToken() == new_access
        assert api.readOnly is True
        assert api.getCarApiBaseURL() == OWNER_API_URL

    def test_fleet_refresh_over_fresh_owner_bearer(self, requests_mock):
        new_access = fleet_token()
        fleet = requests_mock.post(FLEET_REFRESH_URL, json=token_response(new_access))
        owner = requests_mock.post(AUTH_TOKEN_URL, json={})
        api = new_api()
        api.setCarApiBearerToken(owner_token())

        self.save_settings(api, {"carApiRefreshToken": OPAQUE_REFRESH})

        assert owner.call_count == 0
        assert fleet.call_count == 1
        assert fleet.last_request.json()["refresh_token"] == OPAQUE_REFRESH
        assert api.getCarApiBearerToken() == new_access
        assert api.readOnly is False

    def test_paste_back_over_stale_fleet_bearer(self, requests_mock):
        owner = requests_mock.post(AUTH_TOKEN_URL, json=token_response(owner_token()))
        fleet = requests_mock.post(FLEET_REFRESH_URL, json={})
        api = new_api()
        api.setCarApiBearerToken(fleet_token(expires_in=-60))

        assert api.saveApiToken(owner_refresh_token()) == "success"

        assert fleet.call_count == 0
        assert owner.call_count == 1
        assert api.readOnly is True

    def test_both_tokens_pasted_access_token_first(self, requests_mock):
        # The Settings form posts the access token field first, so the
        # refresh token belongs to it: no clear and no refresh.
        owner = requests_mock.post(AUTH_TOKEN_URL, json={})
        fleet = requests_mock.post(FLEET_REFRESH_URL, json={})
        api = new_api()
        api.setCarApiBearerToken(fleet_token(expires_in=-60))
        access, refresh = owner_token(), owner_refresh_token()

        self.save_settings(
            api, {"carApiBearerToken": access, "carApiRefreshToken": refresh}
        )

        assert owner.called is False
        assert fleet.called is False
        assert api.getCarApiBearerToken() == access
        assert api.getCarApiRefreshToken() == refresh
        assert api.readOnly is True


class TestNoClientID:
    def test_owner_refresh_needs_no_client_id(self, requests_mock):
        owner = requests_mock.post(AUTH_TOKEN_URL, json=token_response(owner_token()))
        api = new_api(teslaApiClientID="")

        assert api.setCarApiRefreshToken(owner_refresh_token()) is True

        assert owner.call_count == 1
        assert owner.last_request.json()["client_id"] == "ownerapi"
        assert api.readOnly is True

    def test_fleet_refresh_refused(self, requests_mock, caplog):
        fleet = requests_mock.post(FLEET_REFRESH_URL, json={})
        owner = requests_mock.post(AUTH_TOKEN_URL, json={})
        api = new_api(teslaApiClientID="")

        assert api.setCarApiRefreshToken(OPAQUE_REFRESH) is False

        assert fleet.call_count == 0
        assert owner.call_count == 0
        assert "no teslaApiClientID configured" in caplog.text


class TestRefreshNeverSent:
    @pytest.mark.parametrize(
        "make_access, url",
        [(owner_token, AUTH_TOKEN_URL), (fleet_token, FLEET_REFRESH_URL)],
        ids=["owner", "fleet"],
    )
    def test_connection_error_does_not_crash(self, requests_mock, make_access, url):
        # The request never went out, so there is no response (req is None).
        requests_mock.post(url, exc=requests.exceptions.ConnectionError)
        api = new_api()
        access = make_access(expires_in=60)
        api.setCarApiBearerToken(access)
        api.carApiRefreshToken = "some-refresh"

        assert api.apiRefresh() is False

        # No 401 came back, so the tokens are kept for a later retry.
        assert api.getCarApiBearerToken() == access
        assert api.getCarApiRefreshToken() == "some-refresh"
        assert api.getCarApiLastErrorTime() > 0

    @pytest.mark.parametrize(
        "make_access, url",
        [(owner_token, AUTH_TOKEN_URL), (fleet_token, FLEET_REFRESH_URL)],
        ids=["owner", "fleet"],
    )
    def test_401_clears_both_tokens_and_the_type(self, requests_mock, make_access, url):
        requests_mock.post(url, status_code=401, json={})
        api = new_api()
        api.setCarApiBearerToken(make_access(expires_in=60))
        api.carApiRefreshToken = "some-refresh"
        before = time.time()

        assert api.apiRefresh() is False

        assert api.getCarApiBearerToken() == ""
        assert api.getCarApiRefreshToken() == ""
        assert api.readOnly is False
        assert api.getCarApiBaseURL() == ""
        assert before <= api.getCarApiTokenExpireTime() <= time.time()
        assert {"cmd": "saveSettings"} in api.master.tasks

    def test_401_keeps_the_proxy_for_the_next_fleet_token(self, requests_mock):
        requests_mock.post(AUTH_TOKEN_URL, status_code=401, json={})
        api = new_api(teslaProxy="https://proxy.local:4443")
        api.setCarApiBearerToken(owner_token(expires_in=60))
        api.carApiRefreshToken = "some-refresh"

        api.apiRefresh()
        assert api.getCarApiBaseURL() == ""

        api.setCarApiBearerToken(fleet_token())
        assert api.getCarApiBaseURL() == "https://proxy.local:4443/api/1/vehicles"

    def test_cleared_tokens_save_and_reload_cleared(self, requests_mock, tmp_path):
        # The 401 queues saveSettings; what it saves must load back as "no
        # token", not as the old token or its type.
        from tests.unit.test_teslamate_token_sync import master_with_settings

        requests_mock.post(AUTH_TOKEN_URL, status_code=401, json={})
        refresh = requests_mock.post(FLEET_REFRESH_URL, json={})
        master, api, _ = master_with_settings(tmp_path, {})
        api.setCarApiBearerToken(owner_token(expires_in=60))
        api.carApiRefreshToken = "some-refresh"
        api.apiRefresh()
        master.saveSettings()
        saved = json.loads((tmp_path / "settings.json").read_text())

        master2, api2, _ = master_with_settings(tmp_path, saved)
        master2.loadSettings()

        assert api2.getCarApiBearerToken() == ""
        assert api2.getCarApiRefreshToken() == ""
        assert api2.readOnly is False
        assert api2.getCarApiBaseURL() == ""
        assert refresh.called is False


class TestSyncOn:
    def test_no_refresh_while_sync_on(self, requests_mock):
        owner = requests_mock.post(AUTH_TOKEN_URL, json=token_response(owner_token()))
        fleet = requests_mock.post(FLEET_REFRESH_URL, json={})
        api = new_api()
        api.carApiBearerToken = owner_token(expires_in=60)
        api.readOnly = True
        api.carApiRefreshToken = owner_refresh_token()
        sync_on(api)

        assert api.apiRefresh() is False
        api.refreshTokenIfNeeded()
        assert api.setCarApiRefreshToken(owner_refresh_token()) is False
        assert api.saveApiToken(owner_refresh_token()) == "token_sync"
        assert api.saveApiToken(owner_token()) == "token_sync"
        assert api.pasteRefreshToken(owner_refresh_token()) is False

        assert owner.called is False
        assert fleet.called is False
        assert api.getCarApiBearerToken() != ""
