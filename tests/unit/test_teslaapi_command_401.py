"""
A 401 on a vehicle command (#689), for the cases the provider outcome tests
don't cover. test_provider_outcomes_cloud covers an expired token being
refreshed and the command sent once more, a dead token being retired, and a
403 refusing the command scope without pausing reads.
"""

import re

import pytest

from TWCManager.Vehicle.Reconcile import Outcome as O
from tests.unit.test_provider_outcomes_cloud import calls, fleet
from tests.unit.test_teslaapi_registry import VIN
from tests.unit.teslatokens import fleet_token


@pytest.fixture(autouse=True)
def awake_car(requests_mock):
    # The free vehicle read answers online, so no wake is needed first
    requests_mock.get(
        re.compile(r".*/api/1/vehicles/%s$" % VIN),
        json={"response": {"state": "online"}},
    )


def test_token_replaced_while_command_ran_is_kept(requests_mock):
    # A 401 for a token another thread replaced while the command was out
    # must not clear the new token: the command is sent again with it.
    api, reg, p = fleet(requests_mock)
    replacement = fleet_token(jti="pasted")

    def first(request, context):
        api.storeToken(replacement, "", "pasted")
        context.status_code = 401
        return "invalid bearer token"

    requests_mock.post(
        re.compile(r".*/command/.*"),
        [{"text": first}, {"json": {"response": {"result": True}}}],
    )
    assert p.stopCharging(VIN).kind == O.DONE
    sent = calls(requests_mock, "charge_stop")
    assert len(sent) == 2
    assert sent[1].headers["Authorization"] == "Bearer " + replacement
    assert api.fleetSlot.accessToken == replacement


def test_dead_token_still_cleared_when_unchanged(requests_mock):
    # The same 401 with no replacement retires the token, as before.
    api, reg, p = fleet(requests_mock, status=401)
    assert p.stopCharging(VIN).kind == O.REFUSED
    assert api.fleetSlot.empty()
    assert len(calls(requests_mock, "charge_stop")) == 1


def test_token_cleared_while_command_ran_sends_nothing_more(requests_mock):
    # A 401 for a token another thread cleared while the command was out:
    # nothing is left to send with, so the command is refused at once.
    api, reg, p = fleet(requests_mock)

    def first(request, context):
        api.fleetSlot.clear()
        context.status_code = 401
        return "invalid bearer token"

    requests_mock.post(re.compile(r".*/command/.*"), text=first)
    assert p.stopCharging(VIN) == O.Outcome(O.REFUSED, "401")
    assert len(calls(requests_mock, "charge_stop")) == 1


def test_token_replaced_just_before_the_clear_is_kept(requests_mock):
    # The replacement lands after the 401's first check but before the clear:
    # the clear must refuse to write, and the command goes once more with the
    # new token.
    api, reg, p = fleet(requests_mock)
    replacement = fleet_token(jti="late")
    slot = api.fleetSlot
    clear = slot.clear

    def raced(claimed=None):
        api.storeToken(replacement, "", "pasted")
        return clear(claimed)

    slot.clear = raced
    requests_mock.post(
        re.compile(r".*/command/.*"),
        [{"text": "invalid bearer token", "status_code": 401},
         {"json": {"response": {"result": True}}}],
    )
    assert p.stopCharging(VIN).kind == O.DONE
    sent = calls(requests_mock, "charge_stop")
    assert len(sent) == 2
    assert sent[1].headers["Authorization"] == "Bearer " + replacement
    assert slot.accessToken == replacement
