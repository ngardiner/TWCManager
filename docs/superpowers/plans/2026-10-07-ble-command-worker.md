# Vehicle Command Worker and BLE Serialization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop BLE vehicle commands from blocking TWCManager's serial main loop, and stop concurrent BLE operations from clobbering each other on the shared adapter and key file.

**Architecture:** A new `VehicleCommandQueue` gets its own daemon worker thread. It takes the state-changing vehicle commands (`setChargeRate`, `charge`, `applyChargeLimit`), coalesces pending ones, runs stops first, and calls the existing `VehiclePriority` proxy synchronously, so BLE → Fleet API fallback is unchanged. `TeslaBLE` gains a lock around each "write key file → run `tesla-control` → delete key file" unit. That covers callers that stay on other threads: state reads on the background thread and pairing from HTTP. A main-loop stall warning provides the before/after measurement on the live box.

**Tech Stack:** Python 3 (3.13 on the live box), `threading`, `collections.OrderedDict`, pytest, black.

**Spec:** `docs/superpowers/specs/2026-10-07-ble-command-worker-design.md`

## Global Constraints

- Run all commands from the worktree root: `/home/mbishop/github/TWCManager/.claude/worktrees/ble-threading-twcmanager-62cb96`.
- `SCRATCH=/tmp/claude-1000/-home-mbishop-github-TWCManager--claude-worktrees-ble-threading-twcmanager-62cb96/8798f608-1fae-4176-a592-206045172d8d/scratchpad`. A venv with requirements and test requirements already exists at `$SCRATCH/venv`.
- Run tests with: `$SCRATCH/venv/bin/python -m pytest tests/unit -q -p no:cacheprovider`. `pytest.ini` sets `pythonpath = lib`, so import as `from TWCManager.X import Y`. Baseline: 451 passed.
- Formatting: CI runs `black --exclude="setup.py|tests/*" --check .`. Run `$SCRATCH/venv/bin/black lib/` before each commit that touches `lib/` (install with `$SCRATCH/venv/bin/pip install black` if missing). Tests are not black-checked.
- Loggers come from `LoggerFactory.get_logger(name, category)`.
- No new third-party dependencies.
- Commit messages end with a blank line and then `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Live box: `ssh twcmanager`. Deploys and restarts are pre-approved. Passwordless `sudo` works. Service: `twcmanager.service`. Checkout: `~/TWCManager` (editable install, branch `main` at `a7c84f4`). Rollback: `cd ~/TWCManager && git checkout main && sudo systemctl restart twcmanager`.
- Do not change retry budgets in `VehiclePriority` or `TeslaBLE`.

## File Structure

| File | Responsibility |
|---|---|
| `lib/TWCManager/VehicleCommandQueue.py` (new) | Pending-command tables, coalescing, priority, worker thread, drain. No TWCManager imports except the logger. |
| `lib/TWCManager/TWCMaster.py` | Owns the queue instance; resolves the vehicle module; routes `charge` and `applyChargeLimit` from `queue_background_task`. |
| `lib/TWCManager/TWCManager.py` | Starts the worker; drops the moved background branches; drains stops on shutdown; SIGTERM → graceful shutdown; main-loop stall warning. |
| `lib/TWCManager/TWCSlave.py` | Queues `setChargeRate` instead of calling it; callback-driven `vehicleRateRaised` with a generation guard. |
| `lib/TWCManager/Vehicle/TeslaBLE.py` | `_bleLock` and a `_serialized` decorator on the three adapter-using methods. |
| `tests/unit/test_vehicle_command_queue.py` (new) | Queue behavior. |
| `tests/unit/test_teslable_lock.py` (new) | No overlapping `tesla-control` runs; backoff sleeps happen outside the lock. |
| `tests/unit/test_twcmaster.py` | Routing and module resolution. |
| `tests/unit/test_twcslave.py` | Queueing helpers and the stale-callback guard. |

---

### Task 1: Main-loop stall warning

There is no unit test for this. The main loop is module-level code that exits under pytest (`TWCManager.py:823-828`). Task 2 verifies it on the live box.

**Files:**
- Modify: `lib/TWCManager/TWCManager.py` (the `while True:` main loop, about lines 830-845)

**Interfaces:**
- Produces: log line `Main loop stalled for X.Xs` at WARNING level from the `Manager` logger. Tasks 2 and 9 grep for the exact text `Main loop stalled for`.

- [ ] **Step 1: Add the timestamp before the loop and the check after the existing `now = time.time()`**

Current code (around line 830):

```python
while True:
    try:
        # In this area, we always send a linkready message when we first start.
        ...
        time.sleep(0.025)

        now = time.time()
```

Change it to:

```python
timeLastLoopTop = time.time()
while True:
    try:
        # In this area, we always send a linkready message when we first start.
        ...
        time.sleep(0.025)

        now = time.time()

        # Diagnostic: the serial link must be serviced several times a second.
        # Anything holding this thread for over a second risks losing sync
        # with the slave TWCs.
        if now - timeLastLoopTop > 1.0:
            logger.warning("Main loop stalled for %.1fs", now - timeLastLoopTop)
        timeLastLoopTop = now
```

Leave the existing comment block between `try:` and `time.sleep` in place. Only add the first line before `while True:` and the five lines after `now = time.time()`.

- [ ] **Step 2: Format and run the unit tests**

Run: `$SCRATCH/venv/bin/black lib/ && $SCRATCH/venv/bin/python -m pytest tests/unit -q -p no:cacheprovider 2>&1 | tail -2`
Expected: `451 passed`

- [ ] **Step 3: Commit**

```bash
git add lib/TWCManager/TWCManager.py
git commit -m "Warn when the main loop stalls for over a second

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Live baseline on the box

Deploy only Task 1 (on top of current `main`), then collect metrics while the rest of the work proceeds. Collection runs in the background for 60 minutes.

**Files:**
- Create: `$SCRATCH/live-metrics.sh` (not committed)

**Interfaces:**
- Produces: `$SCRATCH/baseline-pgrep.txt`, `$SCRATCH/baseline-metrics.txt`, and `$SCRATCH/baseline-since.txt` (the restart timestamp). Task 9 produces the same files with an `after-` prefix and compares them.

- [ ] **Step 1: Write the metrics script**

`$SCRATCH/live-metrics.sh`:

```bash
#!/bin/bash
# Usage: live-metrics.sh <since> <until>   (journalctl time specs, e.g. "2026-10-07 19:00:00")
set -u
log=$(mktemp)
ssh twcmanager "journalctl -u twcmanager --since '$1' --until '$2' --no-pager" > "$log"
echo "window: $1 .. $2"
echo "stall warnings: $(grep -c 'Main loop stalled for' "$log")"
echo "worst stall: $(grep -o 'Main loop stalled for [0-9.]*s' "$log" | awk '{print $5}' | sort -n | tail -1)"
echo "key-file errors (no such file or directory): $(grep -ci 'no such file or directory' "$log")"
echo "BLE retry lines: $(grep -c 'retrying in' "$log")"
echo "BLE command timeouts: $(grep -c 'Command timed out' "$log")"
echo "BLE commands exhausted retries: $(grep -c 'after [0-9]* attempts' "$log")"
rm -f "$log"
```

Run: `chmod +x $SCRATCH/live-metrics.sh`

- [ ] **Step 2: Deploy Task 1 to the box**

```bash
git bundle create $SCRATCH/twc.bundle a7c84f4..HEAD
scp -q $SCRATCH/twc.bundle twcmanager:/tmp/twc.bundle
ssh twcmanager 'cd ~/TWCManager && git status --porcelain && git fetch -q /tmp/twc.bundle HEAD && git checkout -q --detach FETCH_HEAD && git log --oneline -1 && sudo systemctl restart twcmanager && date "+%Y-%m-%d %H:%M:%S"'
```

Expected: no porcelain output (clean tree), the Task 1 commit subject, then a timestamp. Save the timestamp: `echo "<timestamp>" > $SCRATCH/baseline-since.txt`.

- [ ] **Step 3: Confirm the service is healthy**

Wait about 30 seconds (use Monitor with an until-loop, not `sleep`), then:

Run: `ssh twcmanager 'systemctl is-active twcmanager; journalctl -u twcmanager --since "-1min" --no-pager | grep -E "Starting main event loop|Traceback|Main loop stalled" | tail -5'`
Expected: `active` and `Starting main event loop...`. Stall warnings during the first seconds are acceptable; a Traceback is not. If a Traceback appears, roll back (see Global Constraints) and stop.

- [ ] **Step 4: Start the 60-minute concurrency sampler (background)**

Run with `run_in_background: true`:

```bash
ssh twcmanager 'end=$((SECONDS+3600)); while [ $SECONDS -lt $end ]; do pgrep -c -f "^[^ ]*tesla-control "; sleep 0.5; done' | sort | uniq -c > $SCRATCH/baseline-pgrep.txt
```

Each output line is `<samples> <concurrent tesla-control processes>`. Any row with a count of 2 or more is an overlap.

- [ ] **Step 5: Generate BLE activity**

During the window, send `checkArrival` three times about 10 minutes apart. Each one runs BLE state reads and charge-limit logic on the background thread:

```bash
ssh twcmanager 'curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:8080/api/checkArrival'
```

Expected: `202`. Do not call `chargeNow` or other endpoints that change charging; natural activity covers the rest. Continue with Task 3 while the window runs.

- [ ] **Step 6: After the sampler finishes, collect metrics**

```bash
$SCRATCH/live-metrics.sh "$(cat $SCRATCH/baseline-since.txt)" "now" > $SCRATCH/baseline-metrics.txt
cat $SCRATCH/baseline-metrics.txt $SCRATCH/baseline-pgrep.txt
```

Record the numbers for the final report. No commit.

---

### Task 3: BLE lock in TeslaBLE

**Files:**
- Modify: `lib/TWCManager/Vehicle/TeslaBLE.py` (imports; `__init__` at line 79; methods `peerWithVehicle` at line 389, `_sendCommand_internal` at line 467, `_fetch_state_json` at line 913)
- Create: `tests/unit/test_teslable_lock.py`

**Interfaces:**
- Produces: `TeslaBLE._bleLock` (`threading.Lock`), and a module-level decorator `_serialized(method)` in `TeslaBLE.py`. No public API change.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_teslable_lock.py`:

```python
"""
Tests that TeslaBLE never runs two tesla-control invocations at once.

Every BLE operation writes key material to one shared path, runs
tesla-control against it, and unlinks it afterwards. Overlapping operations
delete each other's key file, so they must be serialized.
"""

import base64
import os
import threading
import time
from unittest.mock import Mock, patch

import pytest

from TWCManager.Vehicle.TeslaBLE import TeslaBLE

VIN = "5YJ3E1EA1KF000001"


@pytest.fixture
def ble(tmp_path):
    master = Mock()
    master.config = {"config": {"teslaControlPath": "/fake/tesla-control"}}
    master.settings = {
        "Vehicles": {
            VIN: {
                "privKey": base64.b64encode(b"private").decode(),
                "pubKeyPEM": base64.b64encode(b"public").decode(),
            }
        }
    }
    with patch.object(TeslaBLE, "pipeName", str(tmp_path / "ble_data")), patch(
        "TWCManager.Vehicle.TeslaBLE.shutil.which", return_value=None
    ), patch(
        "TWCManager.Vehicle.TeslaBLE.os.path.isfile", return_value=True
    ), patch(
        "TWCManager.Vehicle.TeslaBLE.os.access", return_value=True
    ):
        instance = TeslaBLE(master)
        instance.isDockerCached = False
        yield instance


class OverlapRecorder:
    """Stand-in for _run_command_with_timeout that records concurrency and
    whether the key file still exists while the 'command' runs."""

    def __init__(self, pipe_name, delay=0.05):
        self.pipe_name = pipe_name
        self.delay = delay
        self.active = 0
        self.max_active = 0
        self.missing_key = 0
        self._lock = threading.Lock()

    def __call__(self, command_string, timeout, use_process_group=True):
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        if not os.path.exists(self.pipe_name):
            self.missing_key += 1
        time.sleep(self.delay)
        if not os.path.exists(self.pipe_name):
            self.missing_key += 1
        with self._lock:
            self.active -= 1
        return b"{}", b"ok", 0


def run_concurrently(*calls):
    threads = [threading.Thread(target=c) for c in calls]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)


def test_ble_lock_exists(ble):
    assert hasattr(ble._bleLock, "acquire")
    assert not ble._bleLock.locked()


def test_send_commands_do_not_overlap(ble):
    recorder = OverlapRecorder(ble.pipeName)
    ble._run_command_with_timeout = recorder

    run_concurrently(
        lambda: ble._sendCommand_internal(VIN, "wake"),
        lambda: ble._sendCommand_internal(VIN, "charging-start"),
    )

    assert recorder.max_active == 1
    assert recorder.missing_key == 0


def test_state_read_does_not_overlap_command(ble):
    recorder = OverlapRecorder(ble.pipeName)
    ble._run_command_with_timeout = recorder

    run_concurrently(
        lambda: ble._fetch_state_json(VIN, "charge"),
        lambda: ble._sendCommand_internal(VIN, "charging-set-amps", 16),
    )

    assert recorder.max_active == 1
    assert recorder.missing_key == 0


def test_pairing_does_not_overlap_command(ble):
    recorder = OverlapRecorder(ble.pipeName)
    ble._run_command_with_timeout = recorder

    run_concurrently(
        lambda: ble.peerWithVehicle(VIN),
        lambda: ble._sendCommand_internal(VIN, "wake"),
    )

    assert recorder.max_active == 1
    assert recorder.missing_key == 0


def test_retry_backoff_sleeps_outside_lock(ble):
    ble.retryDelay = 0
    results = iter([(None, None, None), (b"", b"ok", 0)])
    ble._run_command_with_timeout = lambda *a, **k: next(results)
    lock_held_during_sleep = []

    def fake_sleep(seconds):
        lock_held_during_sleep.append(ble._bleLock.locked())

    with patch("TWCManager.Vehicle.TeslaBLE.time.sleep", side_effect=fake_sleep):
        assert ble.sendCommand(VIN, "wake") == "ok"

    assert lock_held_during_sleep == [False]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `$SCRATCH/venv/bin/python -m pytest tests/unit/test_teslable_lock.py -q -p no:cacheprovider 2>&1 | tail -8`
Expected:
- `test_ble_lock_exists` fails with `AttributeError: 'TeslaBLE' object has no attribute '_bleLock'`;
- the three overlap tests fail on `assert recorder.max_active == 1` (2 == 1);
- `test_retry_backoff_sleeps_outside_lock` fails with the same AttributeError.

- [ ] **Step 3: Implement the lock**

In `lib/TWCManager/Vehicle/TeslaBLE.py`:

Add to the imports (alphabetical with the existing ones):

```python
import functools
import threading
```

Add this module-level decorator after `logger = LoggerFactory.get_logger("TeslaBLE", "Vehicle")`:

```python
def _serialized(method):
    """Hold the instance's BLE lock for the whole call.

    Every tesla-control invocation writes key material to the shared
    pipeName path, runs, and unlinks it. Overlapping invocations delete each
    other's key file and contend for the adapter, so each "write key → run →
    unlink" unit must run alone, whatever thread it is called from.
    """

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._bleLock:
            return method(self, *args, **kwargs)

    return wrapper
```

In `TeslaBLE.__init__`, make the lock the first assignment so it exists even when `__init__` returns early:

```python
    def __init__(self, master):
        self._bleLock = threading.Lock()
        self.master = master
```

Decorate the three methods. Do not change their bodies:

```python
    @_serialized
    def peerWithVehicle(self, vin):
```

```python
    @_serialized
    def _sendCommand_internal(self, vin, command, args=None):
```

```python
    @_serialized
    def _fetch_state_json(self, vin, state_type):
```

`_execute_with_retry` calls `_sendCommand_internal` once per attempt and sleeps between attempts, so the backoff already happens outside the lock. None of the three methods calls another of them, so a plain `Lock` (not `RLock`) is correct.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `$SCRATCH/venv/bin/python -m pytest tests/unit/test_teslable_lock.py -q -p no:cacheprovider 2>&1 | tail -2`
Expected: `5 passed`

- [ ] **Step 5: Format, run the full suite, and commit**

Run: `$SCRATCH/venv/bin/black lib/ && $SCRATCH/venv/bin/python -m pytest tests/unit -q -p no:cacheprovider 2>&1 | tail -2`
Expected: `456 passed`

```bash
git add lib/TWCManager/Vehicle/TeslaBLE.py tests/unit/test_teslable_lock.py
git commit -m "Serialize tesla-control invocations in TeslaBLE

Concurrent BLE operations shared the /tmp/ble_data key file and unlinked
it out from under each other, producing retryable 'no such file' failures
that amplified contention. Hold a per-instance lock around each
write-key/run/unlink unit; retry backoff stays outside the lock.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: VehicleCommandQueue

**Files:**
- Create: `lib/TWCManager/VehicleCommandQueue.py`
- Create: `tests/unit/test_vehicle_command_queue.py`

**Interfaces:**
- Produces:
  - `VehicleCommandQueue(resolve_module: Callable[[], object | None])`
  - `.start() -> None`: starts the daemon worker. Idempotent.
  - `.put(task: dict, callback: Callable[[object], None] | None = None) -> None`: non-blocking; never raises.
  - `.drain_high_priority(timeout: float) -> bool`: True if all stops finished and the worker is idle; False on timeout.
  - Task shapes:
    - `{"cmd": "charge", "charge": bool, "vin"?: str}`
    - `{"cmd": "applyChargeLimit", "limit": int}`
    - `{"cmd": "setChargeRate", "amps": int, "vehicle": obj-with-.VIN | str | None}`
  - Dispatch calls on the resolved module:
    - `car_api_charge(task)`
    - `applyChargeLimit(limit=task["limit"])`
    - `setChargeRate(task["amps"], task.get("vehicle"))`

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_vehicle_command_queue.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `$SCRATCH/venv/bin/python -m pytest tests/unit/test_vehicle_command_queue.py -q -p no:cacheprovider 2>&1 | tail -3`
Expected: collection error `ModuleNotFoundError: No module named 'TWCManager.VehicleCommandQueue'`

- [ ] **Step 3: Implement**

`lib/TWCManager/VehicleCommandQueue.py`:

```python
# Serializes state-changing vehicle commands onto one worker thread.
#
# Vehicle commands (especially over BLE) can take minutes when they retry.
# The main loop must keep servicing the TWC serial link, so callers put()
# commands here and return immediately. The worker runs one command at a
# time against the vehicle module (normally VehiclePriority), which keeps
# its synchronous BLE -> Fleet API fallback.
#
# Pending commands coalesce: setChargeRate per vehicle, charge and
# applyChargeLimit per command (matching the background task queue).
# Stop-charging commands run before anything else.
import collections
import threading
import traceback

from TWCManager.Logging.LoggerFactory import LoggerFactory

logger = LoggerFactory.get_logger("VehicleCommandQueue", "Vehicle")


class VehicleCommandQueue:
    _DISPATCH = {
        "charge": lambda module, task: module.car_api_charge(task),
        "applyChargeLimit": lambda module, task: module.applyChargeLimit(
            limit=task["limit"]
        ),
        "setChargeRate": lambda module, task: module.setChargeRate(
            task["amps"], task.get("vehicle")
        ),
    }

    def __init__(self, resolve_module):
        self._resolve_module = resolve_module
        self._cond = threading.Condition()
        self._high = collections.OrderedDict()
        self._low = collections.OrderedDict()
        self._busy = False
        self._draining = False
        self._thread = None

    @staticmethod
    def _key(task):
        cmd = task["cmd"]
        if cmd != "setChargeRate":
            # A vehicle-less charge start applies to every car, so a later
            # stop for one car must replace it rather than run alongside it.
            return (cmd, None)
        vehicle = task.get("vehicle")
        return (cmd, getattr(vehicle, "VIN", vehicle))

    @staticmethod
    def _is_high_priority(task):
        return task["cmd"] == "charge" and "charge" in task and not task["charge"]

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="VehicleCommandQueue", daemon=True
        )
        self._thread.start()

    def put(self, task, callback=None):
        try:
            if not isinstance(task, dict) or "cmd" not in task:
                logger.error("Dropping malformed vehicle command: %r", task)
                return
            key = self._key(task)
            high = self._is_high_priority(task)
            with self._cond:
                if self._draining and not high:
                    logger.debug("Shutting down; dropping vehicle command %r", task)
                    return
                table, other = (
                    (self._high, self._low) if high else (self._low, self._high)
                )
                other.pop(key, None)
                table[key] = (task, callback)
                self._cond.notify_all()
        except Exception:
            logger.error("Failed to queue vehicle command: %s", traceback.format_exc())

    def drain_high_priority(self, timeout):
        with self._cond:
            self._draining = True
            if self._low:
                logger.info(
                    "Shutting down; dropping %d pending vehicle command(s)",
                    len(self._low),
                )
                self._low.clear()
            done = self._cond.wait_for(
                lambda: not self._high and not self._busy, timeout
            )
            if not done:
                logger.warning(
                    "Shutting down with vehicle commands still pending: %r",
                    [task for task, _ in self._high.values()],
                )
            return done

    def _next(self):
        with self._cond:
            self._cond.wait_for(lambda: self._high or self._low)
            table = self._high if self._high else self._low
            _, entry = table.popitem(last=False)
            self._busy = True
            return entry

    def _execute(self, task):
        try:
            handler = self._DISPATCH.get(task["cmd"])
            if handler is None:
                logger.error("Unknown vehicle command %r", task["cmd"])
                return False
            module = self._resolve_module()
            if module is None:
                logger.error("No vehicle module available for %s", task["cmd"])
                return False
            return handler(module, task)
        except Exception as e:
            logger.error(
                f"VehicleCommandError: {traceback.format_exc()}, occurred when processing vehicle command: {e}",
                extra={"colored": "red"},
            )
            return False

    def _run(self):
        while True:
            task, callback = self._next()
            result = self._execute(task)
            if callback is not None:
                try:
                    callback(result)
                except Exception:
                    logger.error(
                        "Vehicle command callback failed: %s", traceback.format_exc()
                    )
            with self._cond:
                self._busy = False
                self._cond.notify_all()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `$SCRATCH/venv/bin/python -m pytest tests/unit/test_vehicle_command_queue.py -q -p no:cacheprovider 2>&1 | tail -2`
Expected: `25 passed`. Then run the file 20 times to shake out timing flakiness:
`for i in $(seq 20); do $SCRATCH/venv/bin/python -m pytest tests/unit/test_vehicle_command_queue.py -q -p no:cacheprovider 2>&1 | tail -1; done | sort | uniq -c`
Expected: a single line, `20 ... 25 passed ...`.

- [ ] **Step 5: Format, run the full suite, and commit**

Run: `$SCRATCH/venv/bin/black lib/ && $SCRATCH/venv/bin/python -m pytest tests/unit -q -p no:cacheprovider 2>&1 | tail -2`
Expected: `481 passed`

```bash
git add lib/TWCManager/VehicleCommandQueue.py tests/unit/test_vehicle_command_queue.py
git commit -m "Add VehicleCommandQueue for off-main-thread vehicle commands

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Wire the queue into TWCMaster and TWCManager

**Files:**
- Modify: `lib/TWCManager/TWCMaster.py` (imports around line 1-17; `__init__` around line 87-130; `queue_background_task` at line 1238)
- Modify: `lib/TWCManager/TWCManager.py` (`background_tasks_thread` at lines 319-323; thread start around line 801; shutdown around line 1947-1954)
- Modify: `tests/unit/test_twcmaster.py` (append a new test class)

**Interfaces:**
- Consumes: `VehicleCommandQueue(resolve_module)`, `.start()`, `.put(task, callback)`, `.drain_high_priority(timeout)` from Task 4.
- Produces:
  - `TWCMaster.vehicleCommandQueue`: a `VehicleCommandQueue` instance, not started.
  - `TWCMaster.queue_vehicle_command(task: dict, callback=None) -> None`
  - `TWCMaster._resolveVehicleCommandModule() -> module | None`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_twcmaster.py`:

```python
class TestVehicleCommandRouting:
    """Vehicle commands go to the VehicleCommandQueue, not the background queue."""

    @pytest.fixture
    def master(self):
        from TWCManager.TWCMaster import TWCMaster

        m = TWCMaster(
            "AB",
            {
                "config": {
                    "debugOutputToFile": False,
                    "subtractChargerLoad": False,
                    "treatGenerationAsGridDelivery": False,
                    "wiringMaxAmpsAllTWCs": 80,
                    "maxAmpsAllowedFromGrid": None,
                }
            },
        )
        m.vehicleCommandQueue = Mock()
        # The background queue state is class-level; isolate it per test.
        m.backgroundTasksQueue = Mock()
        m.backgroundTasksCmds = {}
        m.backgroundTasksDelayed = []
        return m

    def test_master_creates_unstarted_queue(self):
        from TWCManager.TWCMaster import TWCMaster
        from TWCManager.VehicleCommandQueue import VehicleCommandQueue

        m = TWCMaster(
            "AB",
            {
                "config": {
                    "wiringMaxAmpsAllTWCs": 80,
                    "maxAmpsAllowedFromGrid": None,
                }
            },
        )
        assert isinstance(m.vehicleCommandQueue, VehicleCommandQueue)
        assert m.vehicleCommandQueue._thread is None

    @pytest.mark.parametrize(
        "task",
        [
            {"cmd": "charge", "charge": True},
            {"cmd": "charge", "charge": False, "vin": "V1"},
            {"cmd": "applyChargeLimit", "limit": 80},
        ],
    )
    def test_vehicle_commands_routed(self, master, task):
        master.queue_background_task(task)
        master.vehicleCommandQueue.put.assert_called_once_with(task, None)
        master.backgroundTasksQueue.put.assert_not_called()
        assert task["cmd"] not in master.backgroundTasksCmds

    def test_other_tasks_stay_on_background_queue(self, master):
        task = {"cmd": "saveSettings"}
        master.queue_background_task(task)
        master.backgroundTasksQueue.put.assert_called_once_with(task)
        master.vehicleCommandQueue.put.assert_not_called()

    def test_delayed_charge_routed_when_due(self, master):
        task = {"cmd": "charge", "charge": True}
        master.queue_background_task(task, delay=60)
        master.vehicleCommandQueue.put.assert_not_called()
        assert len(master.backgroundTasksDelayed) == 1

        master.backgroundTasksDelayed[0] = (datetime.now() - timedelta(seconds=1), task)
        master.getBackgroundTask()
        master.vehicleCommandQueue.put.assert_called_once_with(task, None)

    def test_queue_vehicle_command_forwards_callback(self, master):
        task = {"cmd": "setChargeRate", "amps": 16, "vehicle": None}
        callback = Mock()
        master.queue_vehicle_command(task, callback)
        master.vehicleCommandQueue.put.assert_called_once_with(task, callback)

    @pytest.mark.parametrize(
        "loaded, expected",
        [
            (["VehiclePriority", "HomeAssistant", "TeslaBLE", "TeslaAPI"], "VehiclePriority"),
            (["HomeAssistant", "TeslaBLE", "TeslaAPI"], "HomeAssistant"),
            (["TeslaBLE", "TeslaAPI"], "TeslaBLE"),
            (["TeslaAPI"], "TeslaAPI"),
            ([], None),
        ],
    )
    def test_resolve_vehicle_command_module(self, master, loaded, expected):
        modules = {name: Mock(name=name) for name in loaded}
        master.getModuleByName = Mock(side_effect=lambda n: modules.get(n))
        resolved = master._resolveVehicleCommandModule()
        assert resolved is (modules[expected] if expected else None)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `$SCRATCH/venv/bin/python -m pytest tests/unit/test_twcmaster.py -q -p no:cacheprovider -k VehicleCommandRouting 2>&1 | tail -3`
Expected: failures with `AttributeError` (`vehicleCommandQueue`, `queue_vehicle_command`, `_resolveVehicleCommandModule`) and `put` call assertions.

- [ ] **Step 3: Implement in TWCMaster**

In `lib/TWCManager/TWCMaster.py`, add the import after `from TWCManager.Logging.LoggerFactory import LoggerFactory`:

```python
from TWCManager.VehicleCommandQueue import VehicleCommandQueue
```

Add a module-level constant after `logger = LoggerFactory.get_logger("Master", "Master")`:

```python
# Background task commands that are vehicle commands. They run on the
# VehicleCommandQueue worker so slow BLE retries don't stall other
# background work.
VEHICLE_COMMANDS = frozenset(["charge", "applyChargeLimit"])
```

In `__init__`, directly after `self.stats = {...}`:

```python
        self.vehicleCommandQueue = VehicleCommandQueue(
            self._resolveVehicleCommandModule
        )
```

In `queue_background_task`, after the `if delay > 0:` block (which returns) and before `self.getBackgroundTasksLock()`:

```python
        if task["cmd"] in VEHICLE_COMMANDS:
            self.queue_vehicle_command(task)
            return
```

Add two methods directly after `queue_background_task`:

```python
    def queue_vehicle_command(self, task, callback=None):
        # Hand a vehicle command to the vehicle command worker. Returns
        # immediately; callback(result) runs on the worker when done.
        self.vehicleCommandQueue.put(task, callback)

    def _resolveVehicleCommandModule(self):
        # Same preference order as TWCSlave.get_vehicle_module.
        for name in ("VehiclePriority", "HomeAssistant", "TeslaBLE", "TeslaAPI"):
            module = self.getModuleByName(name)
            if module:
                return module
        return None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `$SCRATCH/venv/bin/python -m pytest tests/unit/test_twcmaster.py -q -p no:cacheprovider 2>&1 | tail -2`
Expected: all pass.

- [ ] **Step 5: Wire TWCManager.py**

1. In `background_tasks_thread`, delete the two branches that are no longer reachable:

```python
                if task["cmd"] == "applyChargeLimit":
                    vehicleModule.applyChargeLimit(limit=task["limit"])
                elif task["cmd"] == "charge":
                    vehicleModule.car_api_charge(task)
                elif task["cmd"] == "checkArrival":
```

becomes

```python
                if task["cmd"] == "checkArrival":
```

2. After `backgroundTasksThread.start()`:

```python
# Vehicle commands (charge start/stop, charge limit, charge rate) run on
# their own worker so slow BLE retries never block this thread or the
# background thread.
master.vehicleCommandQueue.start()
```

3. At shutdown, before `# Make sure any volatile data is written to disk before exiting`:

```python
# Let pending stop-charging commands reach the car; other pending vehicle
# commands are dropped rather than holding up shutdown.
master.vehicleCommandQueue.drain_high_priority(timeout=60)

```

- [ ] **Step 6: Format, run the full suite, and commit**

Run: `$SCRATCH/venv/bin/black lib/ && $SCRATCH/venv/bin/python -m pytest tests/unit -q -p no:cacheprovider 2>&1 | tail -2`
Expected: `493 passed` (481 + 12 new). If the count differs only because of parametrization, check that there are 0 failures.

```bash
git add lib/TWCManager/TWCMaster.py lib/TWCManager/TWCManager.py tests/unit/test_twcmaster.py
git commit -m "Route charge and charge-limit commands to the vehicle worker

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Graceful shutdown on SIGTERM

`systemctl stop` and `restart` send SIGTERM. Nothing handles it today, so Python exits without running the `KeyboardInterrupt` shutdown path. That means no stop-command drain, no `saveSettings`, and no background-queue join. Map SIGTERM to `KeyboardInterrupt` so that path runs. There is no unit test: the handler is installed in module-level startup code, which exits under pytest. Task 9 verifies it live.

**Files:**
- Modify: `lib/TWCManager/TWCManager.py` (imports at lines 30-42; just before `timeLastLoopTop = time.time()` / `while True:` from Task 1)

**Interfaces:**
- Produces: on SIGTERM, the log line `Exiting after background tasks complete...` (already emitted by the existing `except KeyboardInterrupt:` branch). Task 9 greps for it.

- [ ] **Step 1: Add the import**

Add `import signal` to the import block in `lib/TWCManager/TWCManager.py`, after `import re`.

- [ ] **Step 2: Install the handler just before the main loop**

Directly before the `timeLastLoopTop = time.time()` line added in Task 1:

```python
def sigterm_to_keyboard_interrupt(signum, frame):
    # systemd stop/restart sends SIGTERM. Take the same graceful shutdown
    # path as Ctrl-C: drain stop-charging commands, save settings, and
    # finish background tasks. A second SIGTERM kills immediately.
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    raise KeyboardInterrupt


signal.signal(signal.SIGTERM, sigterm_to_keyboard_interrupt)

```

Python delivers signals to the main thread only, so this raises inside the main loop's `try`, and the existing `except KeyboardInterrupt:` branch handles it. Do not change that branch.

- [ ] **Step 3: Check it by hand locally**

Run:

```bash
PYTHONPATH=lib $SCRATCH/venv/bin/python - <<'PY'
import os, signal, time
def h(signum, frame):
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    raise KeyboardInterrupt
signal.signal(signal.SIGTERM, h)
try:
    os.kill(os.getpid(), signal.SIGTERM)
    time.sleep(1)
except KeyboardInterrupt:
    print("graceful", signal.getsignal(signal.SIGTERM) == signal.SIG_DFL)
PY
```

Expected: `graceful True`. This checks the mechanism; the real check is Task 9.

- [ ] **Step 4: Format, run the full suite, and commit**

Run: `$SCRATCH/venv/bin/black lib/ && $SCRATCH/venv/bin/python -m pytest tests/unit -q -p no:cacheprovider 2>&1 | tail -2`
Expected: `493 passed`

```bash
git add lib/TWCManager/TWCManager.py
git commit -m "Shut down gracefully on SIGTERM

systemctl stop/restart sends SIGTERM, which killed Python without running
the KeyboardInterrupt shutdown path: no settings save, no background-queue
drain. Map the first SIGTERM to KeyboardInterrupt; a second one kills.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: TWCSlave queues setChargeRate

**Files:**
- Modify: `lib/TWCManager/TWCSlave.py` (`__init__` line 82-83; API-control branch lines 860-893; TWC-control raise lines 895-911; add helpers after `get_vehicle_module`)
- Modify: `tests/unit/test_twcslave.py` (append a new test class)

**Interfaces:**
- Consumes: `master.queue_vehicle_command(task, callback=None)` from Task 5.
- Produces:
  - `TWCSlave._queueChargeRate(amps, callback=None)`
  - `TWCSlave._requestVehicleRateRaise()`
  - `TWCSlave._resetVehicleRateRaised()`
  - `TWCSlave._vehicleRateRaiseGen: int`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_twcslave.py`:

```python
class TestVehicleChargeRateQueueing:
    """setChargeRate is queued to the vehicle worker, never called inline."""

    @pytest.fixture
    def vehicle(self):
        v = Mock()
        v.VIN = "5YJ3E1EA1KF000001"
        return v

    @pytest.fixture
    def slave(self, vehicle):
        from TWCManager.TWCSlave import TWCSlave

        master = Mock()
        master.getModuleByName = Mock(return_value=None)
        config = {
            "config": {
                "wiringMaxAmpsPerTWC": 32,
                "useFlexAmpsToStartCharge": False,
                "startStopDelay": 60,
                "fakeMaster": False,
            }
        }
        s = TWCSlave(bytearray(b"AB"), 80, config, master)
        s.getLastVehicle = Mock(return_value=vehicle)
        return s

    def test_queue_charge_rate(self, slave, vehicle):
        slave._queueChargeRate(16.7)
        slave.master.queue_vehicle_command.assert_called_once_with(
            {"cmd": "setChargeRate", "amps": 16, "vehicle": vehicle}, None
        )

    def test_raise_queues_wiring_max_with_callback(self, slave, vehicle):
        slave._requestVehicleRateRaise()
        task, callback = slave.master.queue_vehicle_command.call_args[0]
        assert task == {"cmd": "setChargeRate", "amps": 32, "vehicle": vehicle}
        assert callable(callback)
        assert slave.vehicleRateRaised is False

    @pytest.mark.parametrize("result", [True, "success", None])
    def test_raise_success_sets_flag(self, slave, result):
        slave._requestVehicleRateRaise()
        _, callback = slave.master.queue_vehicle_command.call_args[0]
        callback(result)
        assert slave.vehicleRateRaised is True

    def test_raise_failure_leaves_flag(self, slave):
        slave._requestVehicleRateRaise()
        _, callback = slave.master.queue_vehicle_command.call_args[0]
        callback(False)
        assert slave.vehicleRateRaised is False

    def test_stale_raise_ignored_after_reset(self, slave):
        slave._requestVehicleRateRaise()
        _, callback = slave.master.queue_vehicle_command.call_args[0]
        slave._resetVehicleRateRaised()
        callback(True)
        assert slave.vehicleRateRaised is False

    def test_reset_clears_flag_and_bumps_generation(self, slave):
        slave.vehicleRateRaised = True
        gen = slave._vehicleRateRaiseGen
        slave._resetVehicleRateRaised()
        assert slave.vehicleRateRaised is False
        assert slave._vehicleRateRaiseGen == gen + 1

    def test_heartbeat_code_never_calls_set_charge_rate_inline(self):
        import inspect
        from TWCManager.TWCSlave import TWCSlave

        source = inspect.getsource(TWCSlave)
        assert "vehicleModule.setChargeRate" not in source
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `$SCRATCH/venv/bin/python -m pytest tests/unit/test_twcslave.py -q -p no:cacheprovider -k VehicleChargeRateQueueing 2>&1 | tail -3`
Expected: failures with `AttributeError` for `_queueChargeRate`, `_requestVehicleRateRaise`, `_resetVehicleRateRaised` and `_vehicleRateRaiseGen`, plus the source-inspection assertion.

- [ ] **Step 3: Implement**

In `TWCSlave.__init__`, after `self.vehicleRateRaised = False`:

```python
        # Bumped whenever vehicleRateRaised is reset, so a raise request that
        # completes on the vehicle worker after control moved elsewhere
        # can't set the flag back.
        self._vehicleRateRaiseGen = 0
```

Add these methods directly after `get_vehicle_module`:

```python
    def _queueChargeRate(self, amps, callback=None):
        # Vehicle commands can take minutes over BLE; hand them to the
        # vehicle worker so this (main, serial) thread keeps running.
        self.master.queue_vehicle_command(
            {
                "cmd": "setChargeRate",
                "amps": int(amps),
                "vehicle": self.getLastVehicle(),
            },
            callback,
        )

    def _requestVehicleRateRaise(self):
        gen = self._vehicleRateRaiseGen

        def done(result):
            if result is not False and gen == self._vehicleRateRaiseGen:
                self.vehicleRateRaised = True

        self._queueChargeRate(self.wiringMaxAmps, done)

    def _resetVehicleRateRaised(self):
        self.vehicleRateRaised = False
        self._vehicleRateRaiseGen += 1
```

In the API-control branch (around line 866), replace

```python
            self.APIcontrol = True
            self.vehicleRateRaised = False
            self.__vehicleRateRaiseAttemptTime = 0
```

with

```python
            self.APIcontrol = True
            self._resetVehicleRateRaised()
            self.__vehicleRateRaiseAttemptTime = 0
```

In the same branch (around lines 887-892), replace

```python
                # Determine vehicle to control
                targetVehicle = (
                    None if self.getLastVehicle() is None else self.getLastVehicle()
                )

                self.vehicleModule.setChargeRate(int(desiredAmpsOffered), targetVehicle)
```

with

```python
                self._queueChargeRate(desiredAmpsOffered)
```

In the TWC-control branch (around lines 902-911), replace

```python
            if not self.vehicleRateRaised and (
                now - self.__vehicleRateRaiseAttemptTime >= 60
            ):
                self.__vehicleRateRaiseAttemptTime = now
                result = self.vehicleModule.setChargeRate(
                    self.wiringMaxAmps, self.getLastVehicle()
                )
                if result is not False:
                    self.vehicleRateRaised = True
```

with

```python
            if not self.vehicleRateRaised and (
                now - self.__vehicleRateRaiseAttemptTime >= 60
            ):
                # The 60s window also stops re-requesting while one is queued.
                self.__vehicleRateRaiseAttemptTime = now
                self._requestVehicleRateRaise()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `$SCRATCH/venv/bin/python -m pytest tests/unit/test_twcslave.py -q -p no:cacheprovider 2>&1 | tail -2`
Expected: all pass.

- [ ] **Step 5: Format, run the full suite, and commit**

Run: `$SCRATCH/venv/bin/black lib/ && $SCRATCH/venv/bin/python -m pytest tests/unit -q -p no:cacheprovider 2>&1 | tail -2`
Expected: 0 failures (502 passed).

```bash
git add lib/TWCManager/TWCSlave.py tests/unit/test_twcslave.py
git commit -m "Queue setChargeRate from the heartbeat path instead of calling inline

receive_slave_heartbeat called the vehicle module synchronously on the main
loop; with BLE retries that could block serial servicing for minutes and
lose sync with slave TWCs.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Whole-branch check

**Files:** none modified unless checks fail.

- [ ] **Step 1: Run black in CI mode and the full suite**

Run: `$SCRATCH/venv/bin/black --exclude="setup.py|tests/*" --check . && $SCRATCH/venv/bin/python -m pytest tests/unit -q -p no:cacheprovider 2>&1 | tail -2`
Expected: `All done!` with nothing to reformat, and 0 failures.

- [ ] **Step 2: Grep for leftover inline vehicle calls on the main or background threads**

Run: `grep -rn "vehicleModule\.\(setChargeRate\|car_api_charge\|applyChargeLimit(limit=task\)" lib/`
Expected: no output. (`applyChargeLimit(limit=limit, checkArrival=True)` and `checkDeparture` stay in `background_tasks_thread` by design.)

- [ ] **Step 3: Check the module imports cleanly the way the service loads it**

Run: `PYTHONPATH=lib $SCRATCH/venv/bin/python -c "import TWCManager.TWCMaster, TWCManager.VehicleCommandQueue, TWCManager.Vehicle.TeslaBLE; print('ok')"`
Expected: `ok`

---

### Task 9: Live verification on the box

**Files:** none committed.

**Interfaces:**
- Consumes: `$SCRATCH/live-metrics.sh`, `$SCRATCH/baseline-*.txt` from Task 2.

- [ ] **Step 1: Make sure the baseline window has finished**

`$SCRATCH/baseline-metrics.txt` must exist (Task 2, Step 6). If the sampler is still running, wait for its completion notification.

- [ ] **Step 2: Deploy the branch**

```bash
git bundle create $SCRATCH/twc.bundle a7c84f4..HEAD
scp -q $SCRATCH/twc.bundle twcmanager:/tmp/twc.bundle
ssh twcmanager 'cd ~/TWCManager && git fetch -q /tmp/twc.bundle HEAD && git checkout -q --detach FETCH_HEAD && git log --oneline -1 && sudo systemctl restart twcmanager && date "+%Y-%m-%d %H:%M:%S"'
```

Save the timestamp to `$SCRATCH/after-since.txt`.

- [ ] **Step 3: Health check**

Wait about 30 seconds, then:

Run: `ssh twcmanager 'systemctl is-active twcmanager; journalctl -u twcmanager --since "-1min" --no-pager | grep -E "Starting main event loop|Traceback|VehicleCommandError|Main loop stalled" | tail -5'`
Expected: `active`, `Starting main event loop...`, no Traceback and no VehicleCommandError. On failure, roll back (Global Constraints), capture the log, and stop.

- [ ] **Step 4: Same 60-minute window as the baseline**

Repeat Task 2 Steps 4-6 exactly, writing `$SCRATCH/after-pgrep.txt` and `$SCRATCH/after-metrics.txt` and sending `checkArrival` three times. In addition, at any point when a BLE command is visibly retrying (`retrying in` in `journalctl -f`), confirm from the log that the main loop is not stalled at the same time: no `Main loop stalled for` lines within the retry span.

- [ ] **Step 5: Verify graceful restart**

Run: `ssh twcmanager 'sudo systemctl restart twcmanager; sleep 5; journalctl -u twcmanager --since "-2min" --no-pager | grep -E "Exiting after background tasks complete|dropping .* pending vehicle command|still pending|Traceback|Stopped|Started" | tail -10; systemctl is-active twcmanager'`
Expected:
- `Exiting after background tasks complete...` appears before systemd's `Stopped` line;
- no Traceback;
- the restart completes well under 90 s (no `State 'stop-sigterm' timed out` line);
- the service ends `active`.

If `Exiting after...` is missing, the SIGTERM handler is not working: report it, and do not mark the task done.

- [ ] **Step 6: Compare and report**

Run: `paste $SCRATCH/baseline-metrics.txt $SCRATCH/after-metrics.txt; echo; echo baseline; cat $SCRATCH/baseline-pgrep.txt; echo after; cat $SCRATCH/after-pgrep.txt`

Expected after the change:
- no `pgrep` rows with 2 or more concurrent `tesla-control` processes;
- zero key-file errors;
- no stall warnings that line up with BLE activity (stalls before `Starting main event loop` are startup).

Report both sets of numbers to the user, as observed. If the baseline window had no BLE activity, say so: the comparison is then inconclusive, not a pass.

- [ ] **Step 7: Leave the box on the branch**

Do not roll back if healthy. Tell the user the box is on a detached checkout of the branch, and give the rollback command from Global Constraints.

