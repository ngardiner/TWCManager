"""The Fleet wake path (TeslaAPI.wakeOutcome, through the Fleet provider's
wake): the pre-wake hold, the wake_up, and noticing the car come online
during the post-wake hold.

Live (2026-10-14): wake_up replied 'offline', the car came online about 2
minutes later and stayed up ~12 minutes, but the legacy car_api_available
returned False for the whole 30-minute post-wake hold without looking, so the
car went back to sleep and was woken again every 30 minutes (#699).

These tests were written against car_api_available in #699 and ported when
the reconciler's wake path replaced it. Differences from #699's window:
the free state check is cached 120 s (not polled every 60 s), and it keeps
answering for the whole 30-minute hold, not only its first 10 minutes.
"""

import re
import time

import pytest

from TWCManager.Vehicle.Reconcile import Outcome as O
from TWCManager.Vehicle.Registry.VehicleRegistry import VehicleRegistry
from tests.unit.teslatokens import FLEET_NA_URL, FakeMaster, fleet_token, new_api

# The registry takes only a well-formed VIN.
VIN = "5YJ3E1EA1KF000001"
STATE_URL = FLEET_NA_URL + "/" + VIN
WAKE_URL = STATE_URL + "/wake_up"
HELD = O.Outcome(O.UNREACHABLE, "wake held")
ONLINE = O.Outcome(O.SATISFIED, "online")


class Clock:
    def __init__(self):
        self.now = time.time()

    def __call__(self):
        return self.now

    def advance(self, secs):
        self.now += secs


@pytest.fixture
def clock(monkeypatch):
    import TWCManager.Vehicle.TeslaAPI as teslaapi

    c = Clock()
    monkeypatch.setattr(teslaapi.time, "time", c)
    monkeypatch.setattr(teslaapi.time, "sleep", lambda s: None)
    return c


@pytest.fixture
def car_state(requests_mock):
    """The state GET <vin> reports; set .value to change it."""

    class State:
        value = "asleep"

    def reply(request, context):
        return {"response": {"state": State.value, "vin": VIN}}

    requests_mock.get(STATE_URL, json=reply)
    requests_mock.post(WAKE_URL, json={"response": {"state": "offline"}})
    # Anything else (vehicle_data in particular) must not be requested.
    requests_mock.register_uri("GET", re.compile(r".*/vehicle_data.*"), status_code=599)
    return State


def _api(clock, *tokens):
    # Observations are stamped and aged on the test clock.
    master = FakeMaster()
    master.vehicleRegistry = VehicleRegistry(master.config, clock=clock)
    a = new_api(master)
    for token in tokens:
        a.storeToken(token)
    a.addVehicle({"id": 1, "vin": VIN, "display_name": "Car"})
    return a


@pytest.fixture
def api(clock, car_state):
    return _api(clock, fleet_token())


@pytest.fixture
def fleet(api, clock):
    """The Fleet provider, on the test clock."""
    p = api.providers()[1]
    p.clock = clock
    return p


def _count(requests_mock, method, url):
    return sum(
        1
        for r in requests_mock.request_history
        if r.method == method and r.url.split("?")[0] == url
    )


def _gets(requests_mock):
    return _count(requests_mock, "GET", STATE_URL)


def _wakes(requests_mock):
    return _count(requests_mock, "POST", WAKE_URL)


def _no_vehicle_data(requests_mock):
    return not any("vehicle_data" in r.url for r in requests_mock.request_history)


def _car(api):
    # TeslaAPI's bookkeeping for the car (the wake hold)
    return api.registry.get(VIN).attachment("TeslaAPI")


def _awake(api):
    return api.is_awake(api.registry.get(VIN))


def _teslamate(api, state, stamp):
    # A TeslaMate state observation stamped at `stamp`.
    api.registry.get(VIN).observe("state", state, "TeslaMateVehicle", at=stamp)


def _send_first_wake(api, fleet, clock):
    """Run the pre-wake hold through to the first wake_up. Returns the time
    the wake was sent."""
    assert fleet.wake(VIN) == HELD  # deferring wake
    clock.advance(api.wakeDelayMins * 60)
    assert fleet.wake(VIN) == O.Outcome(O.UNREACHABLE, "offline")
    assert _car(api).delayNextWakeAttempt == 30 * 60
    return clock.now


def test_pre_wake_hold(api, fleet, clock, requests_mock):
    # First ask: one free state check, then hold; no wake_up.
    assert fleet.wake(VIN) == HELD
    assert _gets(requests_mock) == 1
    assert _wakes(requests_mock) == 0
    # During the hold: no wake_up, and the free check at most every 120 s.
    for _ in range(5):
        clock.advance(30)
        assert fleet.wake(VIN) == HELD
    assert _gets(requests_mock) == 2
    assert _wakes(requests_mock) == 0
    # Hold over: exactly one wake.
    clock.advance(api.wakeDelayMins * 60 - 150)
    fleet.wake(VIN)
    assert _wakes(requests_mock) == 1
    assert _no_vehicle_data(requests_mock)


def test_car_online_after_offline_wake_is_noticed(
    api, fleet, clock, car_state, requests_mock
):
    _send_first_wake(api, fleet, clock)
    car = _car(api)

    # The car comes online about 2 minutes after the wake.
    car_state.value = "online"
    clock.advance(2 * 60)
    assert fleet.wake(VIN) == ONLINE

    assert _wakes(requests_mock) == 1
    assert car.firstWakeAttemptTime == 0
    assert car.firstChargeNeededTime == 0
    assert car.delayNextWakeAttempt == 0
    assert _no_vehicle_data(requests_mock)


def test_car_never_online_checks_free_then_wakes_once_per_hold(
    api, fleet, clock, requests_mock
):
    woke = _send_first_wake(api, fleet, clock)
    gets_at_wake = _gets(requests_mock)

    # Asked every 10 s through the hold: one state GET per 120 s cache.
    while clock.now + 10 - woke <= 30 * 60:
        clock.advance(10)
        assert fleet.wake(VIN) == HELD
    assert _gets(requests_mock) - gets_at_wake == (30 * 60) // 120
    assert _wakes(requests_mock) == 1

    # At the retry: exactly one more wake_up.
    clock.now = woke + 30 * 60 + 1
    fleet.wake(VIN)
    assert _wakes(requests_mock) == 2
    assert _no_vehicle_data(requests_mock)


def test_fresh_telemetry_online_needs_no_request(api, fleet, clock, requests_mock):
    _send_first_wake(api, fleet, clock)
    before = len(requests_mock.request_history)
    car = _car(api)

    # TeslaMate reports the car online.
    clock.advance(30)
    _teslamate(api, "online", clock.now)

    assert fleet.wake(VIN) == ONLINE
    assert len(requests_mock.request_history) == before
    assert car.delayNextWakeAttempt == 0


def test_fresh_telemetry_asleep_needs_no_request(api, fleet, clock, requests_mock):
    _send_first_wake(api, fleet, clock)
    before = len(requests_mock.request_history)

    for _ in range(10):
        clock.advance(60)
        _teslamate(api, "asleep", clock.now)
        assert fleet.wake(VIN) == HELD
    assert len(requests_mock.request_history) == before


def test_state_check_with_both_tokens_is_a_state_get(clock, requests_mock):
    from tests.unit.teslatokens import OWNER_API_URL, owner_token

    a = _api(clock, fleet_token(), owner_token())
    p = a.providers()[1]
    p.clock = clock
    requests_mock.get(OWNER_API_URL + "/" + VIN, json={"response": {"state": "asleep"}})
    requests_mock.get(STATE_URL, json={"response": {"state": "asleep"}})
    requests_mock.post(WAKE_URL, json={"response": {"state": "offline"}})
    _send_first_wake(a, p, clock)
    clock.advance(120)
    assert p.wake(VIN) == HELD

    # Only the free state GET (on either account), never vehicle_data: two
    # checks, and the polls after the wake
    gets = [r for r in requests_mock.request_history if r.method == "GET"]
    assert len(gets) == 3 + a.WAKE_POLLS
    assert all(r.url in (STATE_URL, OWNER_API_URL + "/" + VIN) for r in gets)


def test_telemetry_from_before_wake_is_not_trusted(
    api, fleet, clock, car_state, requests_mock
):
    woke = _send_first_wake(api, fleet, clock)
    car = _car(api)
    # TeslaMate's last word is 'asleep', from before the wake we sent: it
    # can't tell us whether the wake worked.
    _teslamate(api, "asleep", woke - 10)

    car_state.value = "online"
    clock.advance(120)
    assert fleet.wake(VIN) == ONLINE
    assert _wakes(requests_mock) == 1
    assert car.delayNextWakeAttempt == 0


def test_stale_telemetry_asleep_does_not_cause_rewakes(
    api, fleet, clock, car_state, requests_mock
):
    # TeslaMate's last 'asleep' is an hour old. The car comes online after
    # one wake_up, but the charge command keeps failing, so every cycle
    # asks again for an hour. Every caller must see the same answer, and the
    # car must not be re-woken at the 30-minute retry.
    _teslamate(api, "asleep", clock.now - 3600)
    start = clock.now
    woke = None
    while clock.now - start <= 60 * 60:
        if woke is None and _wakes(requests_mock) == 1:
            woke = clock.now
        if woke is not None and clock.now - woke >= 120:
            car_state.value = "online"
        if fleet.wake(VIN) == ONLINE:
            assert _awake(api) is True
        clock.advance(10)
    assert _wakes(requests_mock) == 1


def test_fresh_telemetry_newer_than_our_get_is_trusted(api, clock, requests_mock):
    requests_mock.get(STATE_URL, json={"response": {"state": "online"}})
    assert _awake(api) is True
    gets = _gets(requests_mock)

    clock.advance(300)
    _teslamate(api, "asleep", clock.now)
    assert _awake(api) is False
    assert _gets(requests_mock) == gets


def test_our_get_newer_than_telemetry_wins(api, clock, car_state, requests_mock):
    car_state.value = "online"
    assert _awake(api) is True
    gets = _gets(requests_mock)

    # TeslaMate's fresh 'asleep' is older than our GET.
    _teslamate(api, "asleep", clock.now - 1)
    clock.advance(5)
    assert _awake(api) is True
    assert _gets(requests_mock) == gets
    # Once our result is past its cache, GET again rather than fall back
    # to the older telemetry.
    clock.advance(120)
    assert _awake(api) is True
    assert _gets(requests_mock) == gets + 1


def test_stale_telemetry_asleep_falls_through_to_get(
    api, clock, car_state, requests_mock
):
    # An hour-old 'asleep' with no GET of our own yet: telemetry is stale,
    # so check the car (free state GET) instead of trusting it.
    _teslamate(api, "asleep", clock.now - 3600)
    car_state.value = "online"
    assert _awake(api) is True
    assert _gets(requests_mock) == 1


def _poll_sleeps(monkeypatch, clock, car_state, online_after=None):
    # time.sleep advances the test clock; the car connects during the
    # online_after-th sleep. Returns the list of sleeps taken
    import TWCManager.Vehicle.TeslaAPI as teslaapi

    sleeps = []

    def sleep(secs):
        sleeps.append(secs)
        clock.advance(secs)
        if online_after is not None and len(sleeps) >= online_after:
            car_state.value = "online"

    monkeypatch.setattr(teslaapi.time, "sleep", sleep)
    return sleeps


def test_offline_wake_answer_then_online_is_done(
    api, fleet, clock, car_state, requests_mock, monkeypatch
):
    # wake_up answers the state at that moment ('offline'); the car connects
    # seconds later, and the free check after it finds the car online
    assert fleet.wake(VIN) == HELD
    clock.advance(api.wakeDelayMins * 60)
    gets = _gets(requests_mock)
    _poll_sleeps(monkeypatch, clock, car_state, online_after=2)
    assert fleet.wake(VIN) == O.Outcome(O.DONE)
    assert _wakes(requests_mock) == 1
    assert _gets(requests_mock) - gets == 1 + 2  # the pre-wake check, two polls
    car = _car(api)
    assert car.delayNextWakeAttempt == 0 and car.firstWakeAttemptTime == 0
    assert car.online is True
    # Cached from the poll that found it online, before the 5 s settle
    assert car.onlineCheckedAt == clock.now - 5
    assert _no_vehicle_data(requests_mock)


def test_wake_that_never_comes_online_fails_after_bounded_polls(
    api, fleet, clock, car_state, requests_mock, monkeypatch
):
    assert fleet.wake(VIN) == HELD
    clock.advance(api.wakeDelayMins * 60)
    gets = _gets(requests_mock)
    sleeps = _poll_sleeps(monkeypatch, clock, car_state)
    assert fleet.wake(VIN) == O.Outcome(O.UNREACHABLE, "offline")
    assert _wakes(requests_mock) == 1
    polls = _gets(requests_mock) - gets - 1
    assert polls == len(sleeps) and 0 < sum(sleeps) <= 60
    assert _car(api).delayNextWakeAttempt == 30 * 60
    # The back-off holds the next wake_up
    clock.advance(60)
    assert fleet.wake(VIN) == HELD
    assert _wakes(requests_mock) == 1
    assert _no_vehicle_data(requests_mock)


def test_slow_polls_stop_at_the_deadline(
    api, fleet, clock, car_state, requests_mock, monkeypatch
):
    # Each poll takes 20 s (a slow or retried GET): the polls stop once
    # about 45 s have passed, not after a fixed count
    import TWCManager.Vehicle.TeslaAPI as teslaapi

    monkeypatch.setattr(teslaapi.time, "monotonic", clock)
    assert fleet.wake(VIN) == HELD
    clock.advance(api.wakeDelayMins * 60)
    gets = _gets(requests_mock)
    _poll_sleeps(monkeypatch, clock, car_state)
    requests_mock.get(
        STATE_URL,
        json=lambda req, ctx: (
            clock.advance(15),
            {"response": {"state": "asleep", "vin": VIN}},
        )[1],
    )
    assert fleet.wake(VIN) == O.Outcome(O.UNREACHABLE, "offline")
    polls = _gets(requests_mock) - gets - 1
    assert polls == 3  # at +20, +40 and +60 s; none after the 45 s deadline
    assert _wakes(requests_mock) == 1
