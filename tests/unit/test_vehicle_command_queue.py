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

    def applyChargeLimit(self, limit):
        return self._record("applyChargeLimit", limit)

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
        assert module.calls == [("applyChargeLimit", 80)]

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
            ("applyChargeLimit", 80),
            ("setChargeRate", 12, "V1"),
            ("setChargeRate", 8, "V2"),
        ]

    def test_stop_replaces_pending_vehicleless_start(self, q, module):
        block_worker(q, module, {"cmd": "applyChargeLimit", "limit": 80})
        q.put({"cmd": "charge", "charge": True})
        q.put({"cmd": "charge", "charge": False, "vin": "V1"})
        module.gate.set()
        wait_idle(q)
        assert module.calls == [("applyChargeLimit", 80), ("charge", False, "V1")]

    def test_start_replaces_pending_stop(self, q, module):
        block_worker(q, module, {"cmd": "applyChargeLimit", "limit": 80})
        q.put({"cmd": "charge", "charge": False, "vin": "V1"})
        q.put({"cmd": "charge", "charge": True})
        module.gate.set()
        wait_idle(q)
        assert module.calls == [("applyChargeLimit", 80), ("charge", True, None)]

    def test_apply_charge_limit_latest_wins(self, q, module):
        block_worker(q, module, {"cmd": "setChargeRate", "amps": 6, "vehicle": None})
        q.put({"cmd": "applyChargeLimit", "limit": 70})
        q.put({"cmd": "applyChargeLimit", "limit": 90})
        module.gate.set()
        wait_idle(q)
        assert module.calls == [
            ("setChargeRate", 6, None),
            ("applyChargeLimit", 90),
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
            ("applyChargeLimit", 80),
        ]

    def test_start_is_not_high_priority(self, q, module):
        block_worker(q, module, {"cmd": "setChargeRate", "amps": 6, "vehicle": None})
        q.put({"cmd": "applyChargeLimit", "limit": 80})
        q.put({"cmd": "charge", "charge": True})
        module.gate.set()
        wait_idle(q)
        assert module.calls == [
            ("setChargeRate", 6, None),
            ("applyChargeLimit", 80),
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
        def boom(limit):
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
        assert module.calls == [("applyChargeLimit", 80), ("setChargeRate", 16, None)]

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
        assert module.calls == [("applyChargeLimit", 70)]

    def test_unknown_command_gives_false(self, q, module):
        results = []
        q.put({"cmd": "selfDestruct"}, results.append)
        wait_idle(q)
        assert results == [False]
        assert module.calls == []

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
