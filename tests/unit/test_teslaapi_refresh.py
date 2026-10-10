"""
Tesla API token refresh failures.
"""

import requests

from tests.unit import teslatokens
from tests.unit.teslatokens import (
    FLEET_REFRESH_URL,
    fleet_token,
)


def new_api(**config_overrides):
    """A TeslaAPI for a confidential Fleet client. 1.3.4 refreshes a
    public-client (PKCE) Fleet token at auth.tesla.com, so the client secret
    keeps the Fleet refresh at fleet-auth."""
    config_overrides.setdefault("teslaApiClientSecret", "my-client-secret")
    return teslatokens.new_api(**config_overrides)


class TestRefreshNeverSent:
    def test_connection_error_does_not_crash(self, requests_mock):
        # The request never went out, so there is no response (req is None).
        requests_mock.post(FLEET_REFRESH_URL, exc=requests.exceptions.ConnectionError)
        api = new_api()
        access = fleet_token(expires_in=60)
        api.setCarApiBearerToken(access)
        api.carApiRefreshToken = "some-refresh"

        assert api.apiRefresh() is False

        # No 401 came back, so the tokens are kept for a later retry.
        assert api.getCarApiBearerToken() == access
        assert api.getCarApiRefreshToken() == "some-refresh"
        assert api.getCarApiLastErrorTime() > 0

    def test_401_still_clears_tokens(self, requests_mock):
        requests_mock.post(FLEET_REFRESH_URL, status_code=401, json={})
        api = new_api()
        api.setCarApiBearerToken(fleet_token(expires_in=60))
        api.carApiRefreshToken = "some-refresh"

        assert api.apiRefresh() is False

        # The guard keeps the 401 handling. (setCarApiBearerToken("") leaves
        # the bearer token in place, as in release/1.3.3.)
        assert api.getCarApiRefreshToken() == ""
