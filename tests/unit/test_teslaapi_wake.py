"""TeslaAPI.car_api_available wake logic: the pre-wake hold, the wake_up, and
noticing the car come online during the post-wake hold.

Live (2026-10-14): wake_up replied 'offline', the car came online about 2
minutes later and stayed up ~12 minutes, but car_api_available returned False
for the whole 30-minute post-wake hold without looking, so the car went back
to sleep and was woken again every 30 minutes.
"""

import re
import time

import pytest

from TWCManager.Vehicle.Registry.Sources import TeslaBLEReadSource
from TWCManager.Vehicle.Registry.VehicleRegistry import VehicleRegistry
from tests.unit.teslatokens import (  # noqa: F401 (no_threads is a fixture)
    FLEET_NA_URL,
    FakeMaster,
    fleet_token,
    new_api,
    no_threads,
)

# The registry takes only a well-formed VIN.
VIN = "5YJ3E1EA1KF000001"
STATE_URL = FLEET_NA_URL + "/" + VIN
WAKE_URL = STATE_URL + "/wake_up"


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


@pytest.fixture
def api(no_threads, clock, car_state):
    # Observations are stamped and aged on the test clock.
    master = FakeMaster()
    master.vehicleRegistry = VehicleRegistry(master.config, clock=clock)
    a = new_api(master)
    a.storeToken(fleet_token())
    a.addVehicle({"id": 1, "vin": VIN, "display_name": "Car"})
    vehicle = a.getCarApiVehicles()[0]
    vehicle.atHome = True
    return a


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


def _teslamate(vehicle, state, stamp):
    # A TeslaMate state observation stamped at `stamp`.
    vehicle.vehicle.observe("state", state, "TeslaMateVehicle", at=stamp)


def _send_first_wake(api, clock):
    """Run the pre-wake hold through to the first wake_up. Returns the time
    the wake was sent."""
    assert api.car_api_available(charge=True) is False  # deferring wake
    clock.advance(api.wakeDelayMins * 60)
    # The call that sends wake_up returns True (no API error); unchanged.
    api.car_api_available(charge=True)
    assert api.getCarApiVehicles()[0].delayNextWakeAttempt == 30 * 60
    return clock.now


def test_pre_wake_hold_unchanged(api, clock, requests_mock):
    # First call: one state check, then defer; no wake_up.
    assert api.car_api_available(charge=True) is False
    assert _gets(requests_mock) == 1
    assert _wakes(requests_mock) == 0
    # During the hold: no API calls at all.
    for _ in range(5):
        clock.advance(30)
        assert api.car_api_available(charge=True) is False
    assert len(requests_mock.request_history) == 1
    # Hold over: check once more, then wake.
    clock.advance(api.wakeDelayMins * 60 - 150)
    api.car_api_available(charge=True)
    assert _gets(requests_mock) == 2
    assert _wakes(requests_mock) == 1


def test_car_online_after_offline_wake_is_noticed(api, clock, car_state, requests_mock):
    _send_first_wake(api, clock)
    vehicle = api.getCarApiVehicles()[0]
    assert _wakes(requests_mock) == 1
    assert vehicle.delayNextWakeAttempt == 30 * 60

    # The car comes online about 2 minutes after the wake.
    car_state.value = "online"
    clock.advance(2 * 60)
    assert api.car_api_available(charge=True) is True

    assert _wakes(requests_mock) == 1
    assert vehicle.firstWakeAttemptTime == 0
    assert vehicle.firstChargeNeededTime == 0
    assert vehicle.delayNextWakeAttempt == 0
    assert _no_vehicle_data(requests_mock)


def test_car_never_online_polls_in_window_then_waits(api, clock, requests_mock):
    woke = _send_first_wake(api, clock)
    gets_at_wake = _gets(requests_mock)
    poll = api.POST_WAKE_POLL_SECS
    window = api.POST_WAKE_CHECK_SECS

    # Inside the window, called every 10 s: one state GET per poll interval.
    while clock.now + 10 - woke <= window:
        clock.advance(10)
        assert api.car_api_available(charge=True) is False
    window_gets = _gets(requests_mock) - gets_at_wake
    assert window_gets == window // poll
    assert _wakes(requests_mock) == 1

    # After the window, no API calls until the 30-minute retry.
    before = len(requests_mock.request_history)
    while clock.now + 10 - woke <= 30 * 60:
        clock.advance(10)
        assert api.car_api_available(charge=True) is False
    assert len(requests_mock.request_history) == before

    # At the retry: one state check, then exactly one more wake_up.
    clock.now = woke + 30 * 60 + 1
    api.car_api_available(charge=True)
    assert _wakes(requests_mock) == 2
    assert len(requests_mock.request_history) == before + 2
    assert _no_vehicle_data(requests_mock)


def test_fresh_telemetry_online_needs_no_request(api, clock, requests_mock):
    _send_first_wake(api, clock)
    before = len(requests_mock.request_history)
    vehicle = api.getCarApiVehicles()[0]

    # TeslaMate reports the car online.
    clock.advance(30)
    _teslamate(vehicle, "online", clock.now)

    assert api.car_api_available(charge=True) is True
    assert len(requests_mock.request_history) == before
    assert vehicle.delayNextWakeAttempt == 0


def test_fresh_telemetry_asleep_needs_no_request(api, clock, requests_mock):
    _send_first_wake(api, clock)
    before = len(requests_mock.request_history)
    vehicle = api.getCarApiVehicles()[0]

    for _ in range(10):
        clock.advance(60)
        _teslamate(vehicle, "asleep", clock.now)
        assert api.car_api_available(charge=True) is False
    assert len(requests_mock.request_history) == before


def test_state_check_uses_owner_slot_when_present(no_threads, clock, requests_mock):
    from tests.unit.teslatokens import OWNER_API_URL, owner_token

    a = new_api()
    a.storeToken(fleet_token())
    a.storeToken(owner_token())
    a.addVehicle({"id": 1, "vin": VIN, "display_name": "Car"})
    vehicle = a.getCarApiVehicles()[0]
    vehicle.atHome = True
    assert a.slotFor("read") is a.ownerSlot

    requests_mock.get(OWNER_API_URL + "/" + VIN, json={"response": {"state": "asleep"}})
    requests_mock.post(WAKE_URL, json={"response": {"state": "offline"}})
    _send_first_wake(a, clock)
    clock.advance(a.POST_WAKE_POLL_SECS)
    assert a.car_api_available(charge=True) is False

    state_gets = [r for r in requests_mock.request_history if r.method == "GET"]
    assert state_gets
    assert all(r.url == OWNER_API_URL + "/" + VIN for r in state_gets)
    assert _count(requests_mock, "GET", OWNER_API_URL + "/" + VIN) == 3


def test_telemetry_from_before_wake_is_not_trusted(
    api, clock, car_state, requests_mock
):
    woke = _send_first_wake(api, clock)
    vehicle = api.getCarApiVehicles()[0]
    # TeslaMate's last word is 'asleep', from before the wake we sent: it
    # can't tell us whether the wake worked.
    _teslamate(vehicle, "asleep", woke - 10)

    car_state.value = "online"
    clock.advance(api.POST_WAKE_POLL_SECS)
    assert api.car_api_available(charge=True) is True
    assert _wakes(requests_mock) == 1
    assert vehicle.delayNextWakeAttempt == 0


class _FakeBLE:
    def get_location_state(self, vin):
        return {"latitude": 37.0, "longitude": -122.0, "gpsAsOf": 0}

    def wakeVehicle(self, vin):
        raise AssertionError("no BLE wake expected")


def test_ble_read_does_not_move_wake_window(api, clock, requests_mock):
    woke = _send_first_wake(api, clock)
    vehicle = api.getCarApiVehicles()[0]
    ble = _FakeBLE()
    api.master.modules["TeslaBLE"] = ble
    # Reads cascade BLE first for a car TeslaBLE has claimed.
    api.registry.registerReadSource(TeslaBLEReadSource(ble, api.master))
    vehicle.vehicle.claim("TeslaBLE", True, "paired")

    # A BLE location read inside the window touches lastAPIAccessTime.
    clock.advance(300)
    assert vehicle.update_location() is True
    assert vehicle.lastAPIAccessTime == clock.now

    # The window still ends 600 s after the wake: no state GET after it.
    clock.now = woke + 600
    api.car_api_available(charge=True)
    gets = _gets(requests_mock)
    clock.now = woke + 700
    assert api.car_api_available(charge=True) is False
    assert _gets(requests_mock) == gets

    # The retry is still 30 minutes after the wake, not after the BLE read.
    clock.now = woke + 30 * 60 + 1
    api.car_api_available(charge=True)
    assert _wakes(requests_mock) == 2


def test_stale_telemetry_asleep_does_not_cause_rewakes(
    api, clock, car_state, requests_mock
):
    # TeslaMate's last 'asleep' is an hour old. The car comes online after
    # one wake_up, but the charge command keeps failing, so every cycle
    # asks again for an hour. Every caller must see the same answer as the
    # window did, and the car must not be re-woken at the 30-minute retry.
    vehicle = api.getCarApiVehicles()[0]
    _teslamate(vehicle, "asleep", clock.now - 3600)
    start = clock.now
    woke = None
    while clock.now - start <= 60 * 60:
        if woke is None and _wakes(requests_mock) == 1:
            woke = clock.now
        if woke is not None and clock.now - woke >= 120:
            car_state.value = "online"
        if api.car_api_available(charge=True):
            # car_api_charge then checks the car before commanding it.
            vehicle.ready()
        clock.advance(10)
    assert _wakes(requests_mock) == 1


def test_fresh_telemetry_newer_than_our_get_is_trusted(api, clock, requests_mock):
    vehicle = api.getCarApiVehicles()[0]
    car_state_online = {"response": {"state": "online"}}
    requests_mock.get(STATE_URL, json=car_state_online)
    assert vehicle.is_awake() is True
    gets = _gets(requests_mock)

    clock.advance(300)
    _teslamate(vehicle, "asleep", clock.now)
    assert vehicle.is_awake() is False
    assert _gets(requests_mock) == gets


def test_our_get_newer_than_telemetry_wins(api, clock, car_state, requests_mock):
    vehicle = api.getCarApiVehicles()[0]
    _teslamate(vehicle, "asleep", clock.now)

    car_state.value = "online"
    clock.advance(5)
    # E.g. the post-wake window, ignoring telemetry from before the wake.
    assert vehicle.is_awake(telemetryNotBefore=clock.now - 1) is True
    gets = _gets(requests_mock)

    # A default caller: the fresh 'asleep' is older than our GET.
    clock.advance(5)
    assert vehicle.is_awake() is True
    assert _gets(requests_mock) == gets
    # Once our result is past its throttle, GET again rather than fall back
    # to the older telemetry.
    clock.advance(120)
    assert vehicle.is_awake() is True
    assert _gets(requests_mock) == gets + 1


def test_stale_telemetry_asleep_falls_through_to_get(
    api, clock, car_state, requests_mock
):
    # An hour-old 'asleep' with no GET of our own yet: telemetry is stale,
    # so check the car (free state GET) instead of trusting it.
    vehicle = api.getCarApiVehicles()[0]
    _teslamate(vehicle, "asleep", clock.now - 3600)
    car_state.value = "online"
    assert vehicle.is_awake() is True
    assert _gets(requests_mock) == 1
