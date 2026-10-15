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
    a.getCarApiVehicles()[0].atHome = True
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


def _teslamate(car, state, stamp):
    # A TeslaMate state observation stamped at `stamp`.
    car.vehicle.observe("state", state, "TeslaMateVehicle", at=stamp)


def _send_first_wake(api, fleet, clock):
    """Run the pre-wake hold through to the first wake_up. Returns the time
    the wake was sent."""
    assert fleet.wake(VIN) == HELD  # deferring wake
    clock.advance(api.wakeDelayMins * 60)
    assert fleet.wake(VIN) == O.Outcome(O.UNREACHABLE, "offline")
    assert api.getCarApiVehicles()[0].delayNextWakeAttempt == 30 * 60
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
    car = api.getCarApiVehicles()[0]

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
    car = api.getCarApiVehicles()[0]

    # TeslaMate reports the car online.
    clock.advance(30)
    _teslamate(car, "online", clock.now)

    assert fleet.wake(VIN) == ONLINE
    assert len(requests_mock.request_history) == before
    assert car.delayNextWakeAttempt == 0


def test_fresh_telemetry_asleep_needs_no_request(api, fleet, clock, requests_mock):
    _send_first_wake(api, fleet, clock)
    before = len(requests_mock.request_history)
    car = api.getCarApiVehicles()[0]

    for _ in range(10):
        clock.advance(60)
        _teslamate(car, "asleep", clock.now)
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

    # Only the free state GET (on either account), never vehicle_data.
    gets = [r for r in requests_mock.request_history if r.method == "GET"]
    assert len(gets) == 3
    assert all(r.url in (STATE_URL, OWNER_API_URL + "/" + VIN) for r in gets)


def test_telemetry_from_before_wake_is_not_trusted(
    api, fleet, clock, car_state, requests_mock
):
    woke = _send_first_wake(api, fleet, clock)
    car = api.getCarApiVehicles()[0]
    # TeslaMate's last word is 'asleep', from before the wake we sent: it
    # can't tell us whether the wake worked.
    _teslamate(car, "asleep", woke - 10)

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
    car = api.getCarApiVehicles()[0]
    _teslamate(car, "asleep", clock.now - 3600)
    start = clock.now
    woke = None
    while clock.now - start <= 60 * 60:
        if woke is None and _wakes(requests_mock) == 1:
            woke = clock.now
        if woke is not None and clock.now - woke >= 120:
            car_state.value = "online"
        if fleet.wake(VIN) == ONLINE:
            assert car.is_awake() is True
        clock.advance(10)
    assert _wakes(requests_mock) == 1


def test_fresh_telemetry_newer_than_our_get_is_trusted(api, clock, requests_mock):
    car = api.getCarApiVehicles()[0]
    requests_mock.get(STATE_URL, json={"response": {"state": "online"}})
    assert car.is_awake() is True
    gets = _gets(requests_mock)

    clock.advance(300)
    _teslamate(car, "asleep", clock.now)
    assert car.is_awake() is False
    assert _gets(requests_mock) == gets


def test_our_get_newer_than_telemetry_wins(api, clock, car_state, requests_mock):
    car = api.getCarApiVehicles()[0]
    car_state.value = "online"
    assert car.is_awake() is True
    gets = _gets(requests_mock)

    # TeslaMate's fresh 'asleep' is older than our GET.
    _teslamate(car, "asleep", clock.now - 1)
    clock.advance(5)
    assert car.is_awake() is True
    assert _gets(requests_mock) == gets
    # Once our result is past its cache, GET again rather than fall back
    # to the older telemetry.
    clock.advance(120)
    assert car.is_awake() is True
    assert _gets(requests_mock) == gets + 1


def test_stale_telemetry_asleep_falls_through_to_get(
    api, clock, car_state, requests_mock
):
    # An hour-old 'asleep' with no GET of our own yet: telemetry is stale,
    # so check the car (free state GET) instead of trusting it.
    car = api.getCarApiVehicles()[0]
    _teslamate(car, "asleep", clock.now - 3600)
    car_state.value = "online"
    assert car.is_awake() is True
    assert _gets(requests_mock) == 1
