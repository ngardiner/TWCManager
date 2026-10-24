"""
Unit tests for VehicleCommandQueue: dispatch, coalescing, priority,
callbacks, error isolation and shutdown drain.
"""

import threading
import time

import pytest

from TWCManager.VehicleCommandQueue import VehicleCommandQueue

WAIT = 2.0


class Vehicle:
    def __init__(self, vin):
        self.VIN = vin


class RecordingModule:
    """Fake vehicle module. Records calls; blocks each call on `gate` when
    the gate is cleared, so tests can queue work behind an in-flight call."""

    def __init__(self):
        self.calls = []
        self.result = True
        self.gate = threading.Event()
        self.gate.set()
        self.entered = threading.Event()
        self._lock = threading.Lock()

    def _record(self, *call):
        with self._lock:
            self.calls.append(call)
        self.entered.set()
        self.gate.wait(WAIT)
        return self.result

    def car_api_charge(self, task):
        return self._record("charge", task.get("charge"), task.get("vin"))

    def applyChargeLimit(self, limit, vin=None, remoteOnly=False):
        return self._record("applyChargeLimit", limit, vin, remoteOnly)

    def setChargeRate(self, amps, vehicle=None):
        return self._record("setChargeRate", amps, getattr(vehicle, "VIN", vehicle))


def wait_idle(q):
    with q._cond:
        assert q._cond.wait_for(
            lambda: not q._high and not q._low and not q._busy, WAIT
        )


@pytest.fixture
def module():
    return RecordingModule()


@pytest.fixture
def q(module):
    queue = VehicleCommandQueue(lambda: module)
    queue.start()
    return queue


def block_worker(q, module, task):
    """Queue `task` and wait until the worker is executing it, held on the gate."""
    module.gate.clear()
    module.entered.clear()
    q.put(task)
    assert module.entered.wait(WAIT)


class TestDispatch:
    def test_charge(self, q, module):
        q.put({"cmd": "charge", "charge": True, "vin": "V1"})
        wait_idle(q)
        assert module.calls == [("charge", True, "V1")]

    def test_apply_charge_limit(self, q, module):
        q.put({"cmd": "applyChargeLimit", "limit": 80})
        wait_idle(q)
        assert module.calls == [("applyChargeLimit", 80, None, False)]

    def test_set_charge_rate(self, q, module):
        q.put({"cmd": "setChargeRate", "amps": 16, "vehicle": Vehicle("V1")})
        wait_idle(q)
        assert module.calls == [("setChargeRate", 16, "V1")]

    def test_set_charge_rate_without_vehicle(self, q, module):
        q.put({"cmd": "setChargeRate", "amps": 16, "vehicle": None})
        wait_idle(q)
        assert module.calls == [("setChargeRate", 16, None)]

    def test_put_does_not_block_while_worker_busy(self, q, module):
        block_worker(q, module, {"cmd": "applyChargeLimit", "limit": 80})
        start = time.monotonic()
        q.put({"cmd": "setChargeRate", "amps": 16, "vehicle": None})
        assert time.monotonic() - start < 0.1
        module.gate.set()
        wait_idle(q)


class TestCoalescing:
    def test_set_charge_rate_latest_wins_per_vehicle(self, q, module):
        block_worker(q, module, {"cmd": "applyChargeLimit", "limit": 80})
        q.put({"cmd": "setChargeRate", "amps": 10, "vehicle": Vehicle("V1")})
        q.put({"cmd": "setChargeRate", "amps": 8, "vehicle": Vehicle("V2")})
        q.put({"cmd": "setChargeRate", "amps": 12, "vehicle": Vehicle("V1")})
        module.gate.set()
        wait_idle(q)
        assert module.calls == [
            ("applyChargeLimit", 80, None, False),
            ("setChargeRate", 12, "V1"),
            ("setChargeRate", 8, "V2"),
        ]

    def test_apply_charge_limit_latest_wins(self, q, module):
        block_worker(q, module, {"cmd": "setChargeRate", "amps": 6, "vehicle": None})
        q.put({"cmd": "applyChargeLimit", "limit": 70})
        q.put({"cmd": "applyChargeLimit", "limit": 90})
        module.gate.set()
        wait_idle(q)
        assert module.calls == [
            ("setChargeRate", 6, None),
            ("applyChargeLimit", 90, None, False),
        ]

    def test_replaced_callback_not_called(self, q, module):
        block_worker(q, module, {"cmd": "applyChargeLimit", "limit": 80})
        first, second = [], []
        q.put({"cmd": "setChargeRate", "amps": 10, "vehicle": None}, first.append)
        q.put({"cmd": "setChargeRate", "amps": 12, "vehicle": None}, second.append)
        module.gate.set()
        wait_idle(q)
        assert first == []
        assert second == [True]


class TestPriority:
    def test_stop_runs_before_earlier_commands(self, q, module):
        block_worker(q, module, {"cmd": "setChargeRate", "amps": 6, "vehicle": None})
        q.put({"cmd": "setChargeRate", "amps": 16, "vehicle": Vehicle("V1")})
        q.put({"cmd": "applyChargeLimit", "limit": 80})
        q.put({"cmd": "charge", "charge": False, "vin": "V1"})
        module.gate.set()
        wait_idle(q)
        assert module.calls == [
            ("setChargeRate", 6, None),
            ("charge", False, "V1"),
            ("setChargeRate", 16, "V1"),
            ("applyChargeLimit", 80, None, False),
        ]

    def test_start_is_not_high_priority(self, q, module):
        block_worker(q, module, {"cmd": "setChargeRate", "amps": 6, "vehicle": None})
        q.put({"cmd": "applyChargeLimit", "limit": 80})
        q.put({"cmd": "charge", "charge": True})
        module.gate.set()
        wait_idle(q)
        assert module.calls == [
            ("setChargeRate", 6, None),
            ("applyChargeLimit", 80, None, False),
            ("charge", True, None),
        ]


class TestCallbacksAndErrors:
    def test_callback_receives_result(self, q, module):
        results = []
        q.put({"cmd": "applyChargeLimit", "limit": 80}, results.append)
        wait_idle(q)
        module.result = False
        q.put({"cmd": "applyChargeLimit", "limit": 70}, results.append)
        wait_idle(q)
        assert results == [True, False]

    def test_dispatch_exception_gives_false_and_worker_survives(self, q, module):
        def boom(limit, vin=None):
            raise RuntimeError("adapter on fire")

        module.applyChargeLimit = boom
        results = []
        q.put({"cmd": "applyChargeLimit", "limit": 80}, results.append)
        wait_idle(q)
        q.put({"cmd": "setChargeRate", "amps": 16, "vehicle": None})
        wait_idle(q)
        assert results == [False]
        assert module.calls == [("setChargeRate", 16, None)]

    def test_callback_exception_does_not_kill_worker(self, q, module):
        def bad_callback(result):
            raise RuntimeError("callback bug")

        q.put({"cmd": "applyChargeLimit", "limit": 80}, bad_callback)
        wait_idle(q)
        q.put({"cmd": "setChargeRate", "amps": 16, "vehicle": None})
        wait_idle(q)
        assert module.calls == [
            ("applyChargeLimit", 80, None, False),
            ("setChargeRate", 16, None),
        ]

    def test_no_module_gives_false_and_worker_survives(self, module):
        available = []
        q = VehicleCommandQueue(lambda: available[0] if available else None)
        q.start()
        results = []
        q.put({"cmd": "applyChargeLimit", "limit": 80}, results.append)
        wait_idle(q)
        available.append(module)
        q.put({"cmd": "applyChargeLimit", "limit": 70}, results.append)
        wait_idle(q)
        assert results == [False, True]
        assert module.calls == [("applyChargeLimit", 70, None, False)]

    def test_unknown_command_gives_false(self, q, module):
        results = []
        q.put({"cmd": "selfDestruct"}, results.append)
        wait_idle(q)
        assert results == [False]
        assert module.calls == []

    # The worker re-raises SystemExit, which pytest reports as an unhandled
    # thread exception; that is the behaviour under test, not a failure.
    @pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
    def test_base_exception_in_dispatch_clears_busy(self, q, module):
        raised = threading.Event()

        def exit_now(limit, vin=None):
            raised.set()
            raise SystemExit("module called sys.exit")

        module.applyChargeLimit = exit_now
        q.put({"cmd": "applyChargeLimit", "limit": 80})
        assert raised.wait(WAIT)
        start = time.monotonic()
        assert q.drain_high_priority(timeout=WAIT) is True
        assert time.monotonic() - start < 0.5
        # Let the thread finish exiting inside this test.
        q._thread.join(WAIT)

    @pytest.mark.parametrize("task", [None, "charge", {}, {"charge": True}])
    def test_malformed_put_is_dropped_without_raising(self, q, module, task):
        q.put(task)
        wait_idle(q)
        assert module.calls == []


class TestDrain:
    def test_drain_runs_stops_and_drops_the_rest(self, q, module):
        block_worker(q, module, {"cmd": "setChargeRate", "amps": 6, "vehicle": None})
        q.put({"cmd": "applyChargeLimit", "limit": 80})
        q.put({"cmd": "charge", "charge": False, "vin": "V1"})

        outcome = []
        drainer = threading.Thread(
            target=lambda: outcome.append(q.drain_high_priority(timeout=WAIT))
        )
        drainer.start()
        deadline = time.monotonic() + WAIT
        while not q._draining and time.monotonic() < deadline:
            time.sleep(0.005)

        # Arrives during the drain: low priority is dropped.
        q.put({"cmd": "setChargeRate", "amps": 32, "vehicle": None})

        module.gate.set()
        drainer.join(WAIT)
        assert outcome == [True]
        assert module.calls == [("setChargeRate", 6, None), ("charge", False, "V1")]

    def test_drain_accepts_new_stop(self, q, module):
        block_worker(q, module, {"cmd": "setChargeRate", "amps": 6, "vehicle": None})
        outcome = []
        drainer = threading.Thread(
            target=lambda: outcome.append(q.drain_high_priority(timeout=WAIT))
        )
        drainer.start()
        deadline = time.monotonic() + WAIT
        while not q._draining and time.monotonic() < deadline:
            time.sleep(0.005)
        q.put({"cmd": "charge", "charge": False, "vin": "V1"})
        module.gate.set()
        drainer.join(WAIT)
        assert outcome == [True]
        assert module.calls == [("setChargeRate", 6, None), ("charge", False, "V1")]

    def test_drain_times_out(self, q, module):
        block_worker(q, module, {"cmd": "charge", "charge": False, "vin": "V1"})
        assert q.drain_high_priority(timeout=0.1) is False
        module.gate.set()

    def test_drain_when_idle_returns_immediately(self, q):
        start = time.monotonic()
        assert q.drain_high_priority(timeout=WAIT) is True
        assert time.monotonic() - start < 0.5


@pytest.fixture
def unlimited(q, monkeypatch):
    """Turn off the per-car charge interval for tests about other behaviour."""
    monkeypatch.setattr(q, "CHARGE_COMMAND_INTERVAL", 0)
    return q


def stop_for(vin=None):
    """The match TWCMaster.cancelStopCarsCharging uses: pending stops for this
    car and for every car; with no vin, every pending stop."""
    vin = vin or None
    return lambda task: not task.get("charge") and (
        vin is None or task.get("vin") in (vin, None)
    )


class TestKeying:
    def test_charge_keyed_per_vin(self, q, module):
        block_worker(q, module, {"cmd": "applyChargeLimit", "limit": 80})
        q.put({"cmd": "charge", "charge": True, "vin": "V1"})
        q.put({"cmd": "charge", "charge": True, "vin": "V2"})
        q.put({"cmd": "charge", "charge": False, "vin": "V1"})
        module.gate.set()
        wait_idle(q)
        # V1's start was replaced by V1's stop (same key); V2's start survives.
        assert module.calls == [
            ("applyChargeLimit", 80, None, False),
            ("charge", False, "V1"),
            ("charge", True, "V2"),
        ]

    def test_charge_commands_for_different_cars_both_run(self, unlimited, module):
        q = unlimited
        block_worker(q, module, {"cmd": "applyChargeLimit", "limit": 80})
        q.put({"cmd": "charge", "charge": False, "vin": "V1"})
        q.put({"cmd": "charge", "charge": False, "vin": "V2"})
        q.put({"cmd": "charge", "charge": True, "vin": "V3"})
        module.gate.set()
        wait_idle(q)
        assert module.calls == [
            ("applyChargeLimit", 80, None, False),
            ("charge", False, "V1"),
            ("charge", False, "V2"),
            ("charge", True, "V3"),
        ]

    def test_vehicleless_start_replaces_every_pending_stop(self, unlimited, module):
        q = unlimited
        block_worker(q, module, {"cmd": "applyChargeLimit", "limit": 80})
        q.put({"cmd": "charge", "charge": False, "vin": "V1"})
        q.put({"cmd": "charge", "charge": False, "vin": "V2"})
        q.put({"cmd": "charge", "charge": True})
        module.gate.set()
        wait_idle(q)
        assert module.calls == [
            ("applyChargeLimit", 80, None, False),
            ("charge", True, None),
        ]

    def test_stop_for_one_car_replaces_a_vehicleless_start(self, unlimited, module):
        q = unlimited
        block_worker(q, module, {"cmd": "applyChargeLimit", "limit": 80})
        q.put({"cmd": "charge", "charge": True})
        q.put({"cmd": "charge", "charge": True, "vin": "V2"})
        q.put({"cmd": "charge", "charge": False, "vin": "V1"})
        module.gate.set()
        wait_idle(q)
        # The start for every car overlapped V1's stop; V2's own start did not.
        assert module.calls == [
            ("applyChargeLimit", 80, None, False),
            ("charge", False, "V1"),
            ("charge", True, "V2"),
        ]

    def test_empty_vin_means_every_car(self, unlimited, module):
        q = unlimited
        block_worker(q, module, {"cmd": "applyChargeLimit", "limit": 80})
        q.put({"cmd": "charge", "charge": True, "vin": "V1"})
        q.put({"cmd": "charge", "charge": False, "vin": ""})
        module.gate.set()
        wait_idle(q)
        assert module.calls == [
            ("applyChargeLimit", 80, None, False),
            ("charge", False, ""),
        ]

    def test_apply_charge_limit_per_vin_does_not_merge_with_policy_wide(
        self, q, module
    ):
        block_worker(q, module, {"cmd": "setChargeRate", "amps": 6, "vehicle": None})
        q.put({"cmd": "applyChargeLimit", "limit": 70})
        q.put({"cmd": "applyChargeLimit", "limit": 90, "vin": "V1"})
        q.put({"cmd": "applyChargeLimit", "limit": 80})
        module.gate.set()
        wait_idle(q)
        assert module.calls == [
            ("setChargeRate", 6, None),
            ("applyChargeLimit", 80, None, False),
            ("applyChargeLimit", 90, "V1", False),
        ]

    def test_apply_charge_limit_passes_vin_and_remote_only(self, q, module):
        q.put({"cmd": "applyChargeLimit", "limit": -1, "vin": "V1", "remoteOnly": True})
        wait_idle(q)
        assert module.calls == [("applyChargeLimit", -1, "V1", True)]

    def test_apply_charge_limit_without_vin_omits_vin_kwarg(self, q, module):
        calls = []

        def legacy(limit):
            calls.append(limit)

        module.applyChargeLimit = legacy
        q.put({"cmd": "applyChargeLimit", "limit": 80})
        wait_idle(q)
        assert calls == [80]


class TestCancel:
    def test_cancel_removes_matching_pending_entries(self, q, module):
        block_worker(q, module, {"cmd": "setChargeRate", "amps": 6, "vehicle": None})
        q.put({"cmd": "charge", "charge": False, "vin": "V1"})
        q.put({"cmd": "charge", "charge": False, "vin": "V2"})
        q.put({"cmd": "charge", "charge": True, "vin": "V3"})
        removed = q.cancel("charge", lambda task: not task.get("charge"))
        assert removed == 2
        module.gate.set()
        wait_idle(q)
        assert module.calls == [("setChargeRate", 6, None), ("charge", True, "V3")]

    def test_cancel_leaves_running_entry_alone(self, q, module):
        block_worker(q, module, {"cmd": "charge", "charge": False, "vin": "V1"})
        assert q.cancel("charge", lambda task: True) == 0
        module.gate.set()
        wait_idle(q)
        assert module.calls == [("charge", False, "V1")]

    def test_cancel_matches_command_name(self, q, module):
        block_worker(q, module, {"cmd": "applyChargeLimit", "limit": 80})
        q.put({"cmd": "setChargeRate", "amps": 10, "vehicle": None})
        assert q.cancel("charge", lambda task: True) == 0
        module.gate.set()
        wait_idle(q)
        assert module.calls == [
            ("applyChargeLimit", 80, None, False),
            ("setChargeRate", 10, None),
        ]

    def test_cancels_pending_stop_for_the_car(self, unlimited, module):
        q = unlimited
        block_worker(q, module, {"cmd": "applyChargeLimit", "limit": 80})
        q.put({"cmd": "charge", "charge": False, "vin": "V1"})
        q.put({"cmd": "charge", "charge": False, "vin": "V2"})
        assert q.cancel("charge", stop_for("V1")) == 1
        module.gate.set()
        wait_idle(q)
        assert module.calls == [
            ("applyChargeLimit", 80, None, False),
            ("charge", False, "V2"),
        ]

    def test_cancels_a_stop_for_every_car(self, unlimited, module):
        q = unlimited
        block_worker(q, module, {"cmd": "applyChargeLimit", "limit": 80})
        q.put({"cmd": "charge", "charge": False, "vin": None})
        assert q.cancel("charge", stop_for("V1")) == 1
        module.gate.set()
        wait_idle(q)
        assert module.calls == [("applyChargeLimit", 80, None, False)]

    @pytest.mark.parametrize("vin", [None, ""])
    def test_without_vin_cancels_every_stop(self, unlimited, module, vin):
        q = unlimited
        block_worker(q, module, {"cmd": "applyChargeLimit", "limit": 80})
        q.put({"cmd": "charge", "charge": False, "vin": "V1"})
        q.put({"cmd": "charge", "charge": False, "vin": "V2"})
        assert q.cancel("charge", stop_for(vin)) == 2
        module.gate.set()
        wait_idle(q)
        assert module.calls == [("applyChargeLimit", 80, None, False)]

    def test_leaves_starts_and_other_commands(self, unlimited, module):
        q = unlimited
        block_worker(q, module, {"cmd": "setChargeRate", "amps": 6, "vehicle": None})
        q.put({"cmd": "charge", "charge": True, "vin": "V1"})
        q.put({"cmd": "applyChargeLimit", "limit": 80})
        assert q.cancel("charge", stop_for("V1")) == 0
        module.gate.set()
        wait_idle(q)
        assert module.calls == [
            ("setChargeRate", 6, None),
            ("charge", True, "V1"),
            ("applyChargeLimit", 80, None, False),
        ]

    def test_stops_are_not_cancelled_during_shutdown(self, unlimited, module):
        q = unlimited
        block_worker(q, module, {"cmd": "setChargeRate", "amps": 6, "vehicle": None})
        q.put({"cmd": "charge", "charge": False, "vin": "V1"})
        drainer = threading.Thread(target=lambda: q.drain_high_priority(timeout=WAIT))
        drainer.start()
        deadline = time.monotonic() + WAIT
        while not q._draining and time.monotonic() < deadline:
            time.sleep(0.005)
        assert q.cancel("charge", stop_for("V1")) == 0
        module.gate.set()
        drainer.join(WAIT)
        assert module.calls == [("setChargeRate", 6, None), ("charge", False, "V1")]


class TestChargeInterval:
    def test_repeat_within_interval_is_dropped(self, q, module):
        q.put({"cmd": "charge", "charge": False, "vin": "V1"})
        wait_idle(q)
        q.put({"cmd": "charge", "charge": False, "vin": "V1"})
        q.put({"cmd": "charge", "charge": True, "vin": "V1"})
        wait_idle(q)
        assert module.calls == [("charge", False, "V1")]

    def test_other_car_is_not_limited(self, q, module):
        q.put({"cmd": "charge", "charge": False, "vin": "V1"})
        wait_idle(q)
        q.put({"cmd": "charge", "charge": False, "vin": "V2"})
        wait_idle(q)
        assert module.calls == [("charge", False, "V1"), ("charge", False, "V2")]

    def test_vehicleless_command_waits_for_any_car(self, q, module):
        q.put({"cmd": "charge", "charge": False, "vin": "V1"})
        wait_idle(q)
        q.put({"cmd": "charge", "charge": True})
        wait_idle(q)
        assert module.calls == [("charge", False, "V1")]

    def test_runs_again_after_interval(self, q, module, monkeypatch):
        clock = [1000.0]
        monkeypatch.setattr(
            "TWCManager.VehicleCommandQueue.time.monotonic", lambda: clock[0]
        )
        q.put({"cmd": "charge", "charge": False, "vin": "V1"})
        wait_idle(q)
        clock[0] += q.CHARGE_COMMAND_INTERVAL - 1
        q.put({"cmd": "charge", "charge": False, "vin": "V1"})
        wait_idle(q)
        clock[0] += 1
        q.put({"cmd": "charge", "charge": False, "vin": "V1"})
        wait_idle(q)
        assert module.calls == [("charge", False, "V1"), ("charge", False, "V1")]

    def test_failed_attempt_counts(self, q, module):
        module.result = False
        q.put({"cmd": "charge", "charge": False, "vin": "V1"})
        wait_idle(q)
        q.put({"cmd": "charge", "charge": False, "vin": "V1"})
        wait_idle(q)
        assert module.calls == [("charge", False, "V1")]

    def test_shutdown_stop_ignores_interval(self, q, module):
        q.put({"cmd": "charge", "charge": True, "vin": "V1"})
        wait_idle(q)
        q._draining = True
        q.put({"cmd": "charge", "charge": False, "vin": "V1"})
        assert q.drain_high_priority(timeout=WAIT) is True
        assert module.calls == [("charge", True, "V1"), ("charge", False, "V1")]
