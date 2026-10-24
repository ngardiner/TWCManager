"""
Tesla refresh tokens are single-use, so two threads must never spend the same
one (#690). A refresh that waited for another thread's refresh of the slot
uses its result instead of sending the spent token again.
"""

import threading
import time

from tests.unit.teslatokens import (
    FLEET_REFRESH_URL,
    fleet_token,
    new_api,
)

OPAQUE_REFRESH = "NA_0123456789abcdef0123456789abcdef"


def slow_refresh(api, delay=0.2):
    """Make the Fleet slot's refresh request slow, recording each token it
    spends, and answer it with a fresh token pair."""
    spent = []

    def post(url, json=None, **kwargs):
        spent.append(json["refresh_token"])
        time.sleep(delay)

        class Response:
            status_code = 200
            text = (
                '{"access_token": "%s", "refresh_token": "NA_new%d", '
                '"expires_in": 28800}' % (fleet_token(), len(spent))
            )

            def raise_for_status(self):
                pass

        return Response()

    api.fleetSlot.post = post
    return spent


def run_together(*targets):
    threads = [threading.Thread(target=t) for t in targets]
    for t in threads:
        t.start()
        time.sleep(0.02)
    for t in threads:
        t.join(5)


def test_concurrent_refreshes_spend_the_token_once():
    api = new_api()
    api.storeToken(fleet_token(expires_in=60), OPAQUE_REFRESH)
    spent = slow_refresh(api)

    run_together(api.refreshTokenIfNeeded, api.refreshTokenIfNeeded)

    assert spent == [OPAQUE_REFRESH]
    assert api.fleetSlot.refreshToken == "NA_new1"
    assert api.fleetSlot.accessToken


def test_401_refresh_after_another_threads_refresh_is_not_resent():
    # A request that got "token expired" while another thread was already
    # refreshing retries with the new token rather than refreshing again.
    api = new_api()
    api.storeToken(fleet_token(expires_in=60), OPAQUE_REFRESH)
    spent = slow_refresh(api)
    results = []

    run_together(
        api.refreshTokenIfNeeded,
        lambda: results.append(api.refreshSlot(api.fleetSlot)),
    )

    assert spent == [OPAQUE_REFRESH]
    assert results == ["success"]


def test_paste_during_refresh_keeps_the_pasted_token():
    api = new_api()
    api.storeToken(fleet_token(expires_in=60), OPAQUE_REFRESH)
    spent = slow_refresh(api)
    pasted = "NA_pasted0123456789abcdef0123456789"
    results = []

    run_together(
        api.refreshTokenIfNeeded,
        lambda: results.append(api.pasteRefreshToken(pasted)),
    )

    # The paste waits for the refresh in progress, then spends its own token.
    assert spent == [OPAQUE_REFRESH, pasted]
    assert results == ["success"]
    assert api.fleetSlot.refreshToken == "NA_new2"


def test_refresh_waiting_on_a_paste_does_not_spend_the_pasted_token():
    api = new_api()
    api.storeToken(fleet_token(expires_in=60), OPAQUE_REFRESH)
    spent = slow_refresh(api)
    pasted = "NA_pasted0123456789abcdef0123456789"

    run_together(
        lambda: api.pasteRefreshToken(pasted),
        api.refreshTokenIfNeeded,
    )

    assert spent == [pasted]
    assert api.fleetSlot.refreshToken == "NA_new1"
