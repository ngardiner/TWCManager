# Vehicle Command Worker and BLE Serialization — Design

Date: 2026-10-07
Status: Approved approach (C); awaiting spec review

## Problem

TWCManager's main loop services the RS-485 link to Gen2 TWCs and assumes the
work it calls out to is fast. TeslaBLE commands are not: each command shells
out to `tesla-control` with a 5 s timeout, retries up to 4 times with 2/4/8 s
backoff (`TeslaBLE._execute_with_retry`), and `setChargeRate` does this twice
(wake, then `charging-set-amps`). `VehiclePriority` then wraps that in its own
retry loop (priority 20 → 3 attempts). A single `setChargeRate` can block for
minutes.

Two distinct defects result:

1. **Main-thread blocking.** `TWCSlave.receive_slave_heartbeat` calls
   `self.vehicleModule.setChargeRate(...)` synchronously
   (`TWCSlave.py:892` and `:906`). While BLE retries, the serial port is not
   read, and TWCManager loses sync with the slave TWCs. `setChargeRate` is the
   only vehicle command that bypasses the task queue.

2. **Adapter contention.** BLE operations are issued from three thread kinds:
   - main loop: `setChargeRate`;
   - `background_tasks_thread`: `car_api_charge`, `applyChargeLimit`,
     `checkArrival`/`checkDeparture`, and `updateChargeAtHome`, the last of
     which reaches `TeslaBLE.get_charge_state` / `get_location_state` /
     `wakeVehicle` via `TeslaAPI` `update_charge` / `update_location`;
   - HTTP request threads (`ThreadingMixIn`): `peerWithVehicle`
     (`HTTPControl.py:1481`).

   Every operation writes key material to the single shared path
   `/tmp/ble_data`, runs `tesla-control -key-file /tmp/ble_data`, and in
   `finally` unlinks that path. Overlapping callers unlink or overwrite each
   other's key file, `tesla-control` fails with "no such file or directory",
   and `_is_transient_error` classifies that as retryable — so contention
   amplifies itself.

A secondary effect: BLE latency on `background_tasks_thread` delays unrelated
work on that thread (`checkGreenEnergy`, `getVehicleVIN`, `saveSettings`,
`updateStatus`).

## Goals

- Main loop never waits on a vehicle command.
- At most one `tesla-control` invocation in flight at any time, regardless of
  calling thread.
- `VehiclePriority`'s synchronous BLE → Fleet API fallback keeps working
  unchanged.
- Stale vehicle commands are superseded rather than executed in sequence.
- A stop-charging request is not stuck behind slow start / rate commands.

## Non-goals

- Making `TeslaBLE` internally asynchronous. Its methods stay synchronous;
  only *who calls them* changes.
- Changing retry budgets. `VehiclePriority` retry counts and TeslaBLE's
  internal retry/backoff/circuit breaker are unchanged.
- Moving state reads (`checkCharge` / `updateChargeAtHome`,
  `checkArrival`, `checkDeparture`) off `background_tasks_thread`. They stay
  where they are. The BLE lock serializes their use of the adapter, but it
  does not make them race-free: `checkArrival` / `checkDeparture` on the
  background thread can now interleave with Policy's `applyChargeLimit` on
  the vehicle worker. Both touch module bookkeeping (charge-limit flags,
  saving and removing the normal charge limit) and can trigger a TeslaAPI
  token refresh. This is a known race, deferred.
- Moving HTTP-initiated pairing off the request thread. It takes the BLE lock
  and blocks only its own HTTP request, as today.
- Fixing the pre-existing unlocked access to `backgroundTasksDelayed` or the
  unsynchronized `master.stats` counters in `VehiclePriority`.

## Design

### Component 1: `VehicleCommandQueue` (new, `lib/TWCManager/VehicleCommandQueue.py`)

A small, self-contained class owning two pending-command tables (high and
low priority, each a `collections.OrderedDict` keyed by coalescing key), a
`threading.Condition`, and one daemon worker thread.

**Interface**

```python
class VehicleCommandQueue:
    def __init__(self, resolve_module): ...
    def start(self): ...                      # spawns daemon worker thread
    def put(self, task, callback=None): ...   # non-blocking; never raises
    def drain_high_priority(self, timeout): ... # shutdown; see Shutdown
```

- `resolve_module` is a zero-argument callable returning the vehicle module
  to dispatch to (normally `VehiclePriority`). Injected so the class can be
  unit-tested without a `TWCMaster`.
- `task` is a dict with a `cmd` key, same shape as background tasks.
- `callback`, if given, is called on the worker thread as `callback(result)`
  after execution. `result` is the dispatched method's return value, or
  `False` if dispatch raised. Exceptions from the callback are logged and
  swallowed.

**Supported commands and dispatch**

| `cmd`              | Task fields                  | Dispatch                                              |
|--------------------|------------------------------|-------------------------------------------------------|
| `charge`           | `charge` (bool), `vin` opt.  | `module.car_api_charge(task)`                         |
| `applyChargeLimit` | `limit`                      | `module.applyChargeLimit(limit=task["limit"])`        |
| `setChargeRate`    | `amps` (int), `vehicle` opt. | `module.setChargeRate(task["amps"], task.get("vehicle"))` |

`setChargeRate` carries `vehicle` exactly as `TWCSlave.getLastVehicle()`
returned it (a vehicle object or `None`), because module implementations
such as `TeslaAPI.setChargeRate` need the object, not just the VIN. The task
does not duplicate the VIN; the queue derives it when computing the
coalescing key.

Unknown `cmd` values are logged at error level and dropped (callback invoked
with `False`).

**Coalescing**

Pending `setChargeRate` commands are keyed by `(cmd, vin)`, where `vin` is
`task["vehicle"].VIN` (or `None` when no vehicle is given). `charge` and
`applyChargeLimit` are keyed by `cmd` alone, exactly as the background queue
coalesces them today.

Why `charge` is not keyed by VIN: in `chargeStopMode` 1, `startCarsCharging`
queues `charge` without a VIN (meaning all vehicles), while
`stopCarsCharging` queues it with the slave's VIN. Keyed by VIN, a pending
start and a later stop would both run, stop first and then start, which
re-starts the car. Keyed by `cmd`, the later stop replaces the start. Putting a task whose key is already *pending* replaces that
pending task's payload and callback in place (latest wins) and keeps its queue
position, except as described under priority below. A task already being
executed by the worker is not pending; a new put with the same key creates a
new pending entry.

Consequences:
- Repeated `setChargeRate` for one vehicle collapses to the newest amps value.
- `setChargeRate` for different vehicles never clobber each other.
- A pending `charge` is replaced by any later `charge`, so a stop issued while
  a start is still pending replaces it (and vice versa). This matches today's
  behavior.
- A replaced entry's callback is discarded without being called.

**Priority**

A `charge` task with `charge == False` is high priority. Everything else is
low priority. Each task lives in the table for its priority. The worker
takes the oldest entry from the high table if it has any, otherwise the
oldest entry from the low table.

On put, the key is looked up in both tables:
- If the existing entry is in the same table as the new task's priority, its
  payload and callback are replaced and it keeps its position.
- If the existing entry is in the other table (for example, a pending start
  replaced by a stop), it is removed there and the new task is appended to
  the end of its own table.

There is no pre-emption: a command already executing runs to completion.

**Worker loop**

```
loop:
    with cond:
        wait until pending non-empty
        pop next task per priority rule
    module = resolve_module()
    if module is None: result = False (log error)
    else: result = dispatch(module, task)   # wrapped in try/except → False
    if callback: callback(result)            # wrapped in try/except
```

The lock is never held while dispatching or running the callback.

### Component 2: TWCMaster integration

- `TWCMaster.__init__` creates `self.vehicleCommandQueue =
  VehicleCommandQueue(self._resolveVehicleCommandModule)`.
- `_resolveVehicleCommandModule()` returns, in order, the first loaded of
  `VehiclePriority`, `HomeAssistant`, `TeslaBLE`, `TeslaAPI` — the same order
  as `TWCSlave.get_vehicle_module`.
- New `TWCMaster.queue_vehicle_command(task, callback=None)` forwards to
  `self.vehicleCommandQueue.put`.
- `TWCMaster.queue_background_task` routes `cmd` in `{"charge",
  "applyChargeLimit"}` to `queue_vehicle_command` when `delay == 0`. Delayed
  tasks still go to `backgroundTasksDelayed`; when they mature,
  `getBackgroundTask` re-enters `queue_background_task`, which routes them.
  This leaves every existing producer (`startCarsCharging`,
  `stopCarsCharging`, `Policy`) unchanged.
- The `charge` and `applyChargeLimit` branches in `background_tasks_thread`
  are removed (no longer reachable).
- `TWCManager.py` calls `master.vehicleCommandQueue.start()` next to where it
  starts `backgroundTasksThread`.

### Component 3: TWCSlave call sites

- `TWCSlave.py:892` (API-controlled rate): replace the direct call with
  `self.master.queue_vehicle_command({"cmd": "setChargeRate", "amps":
  int(desiredAmpsOffered), "vehicle": targetVehicle})`. The
  existing 15 s / repeat-count throttling stays as is. No callback — the
  return value was already ignored.
- `TWCSlave.py:906` (raise car limit to wiring max): queue
  `{"cmd": "setChargeRate", "amps": self.wiringMaxAmps, ...}` with a callback
  that sets `self.vehicleRateRaised = True` when `result is not False`
  (preserving the existing success test). `__vehicleRateRaiseAttemptTime` is
  still set at enqueue time, so the existing 60 s window prevents
  re-submitting while a request is outstanding. The callback runs on the
  worker thread.
- The API-control branch resets `vehicleRateRaised = False` on the main
  thread. A raise callback that completes after control has switched to API
  mode could set it back to `True`. To prevent that, the callback captures a
  generation counter (`self._vehicleRateRaiseGen`, incremented whenever
  `vehicleRateRaised` is reset) and only sets the flag if the counter is
  unchanged.
- The generation check narrows the race but does not close it: the callback's
  check and its set are not atomic with the main thread's reset, so a reset
  that lands between them can still leave a stale `True`. This self-corrects,
  because the API-control branch resets the flag on every heartbeat. The only
  cost is that one raise may be skipped.

### Component 4: Main-loop stall warning (`TWCManager.py`)

Needed to verify the goal on real hardware. SHB lines are logged only on
change or every 10 minutes, so they cannot show stalls.

At the top of the outer `while True` loop, record `time.time()`. If the gap
since the previous iteration's top exceeds 1.0 s, log a warning:
`Main loop stalled for X.Xs`. In normal operation an iteration takes the
25 ms sleep plus processing, so this does not fire. It stays in the code
permanently as a diagnostic.

### Component 5: BLE lock in TeslaBLE

- Add `self._bleLock = threading.Lock()` in `TeslaBLE.__init__`.
- Hold it around each "write key file → run `tesla-control` → unlink key
  file" unit:
  - `_sendCommand_internal`: from `sendPrivateKey` through the `finally:
    _ensure_pipe_closed()`.
  - `_fetch_state_json`: same span.
  - `peerWithVehicle`: from `sendPublicKey` through its `finally`.
- The retry backoff `time.sleep` in `_execute_with_retry` runs outside the
  lock (it already sits outside `_sendCommand_internal`), so other callers
  can use the adapter while one command backs off.
- No other TeslaBLE behavior changes.

## Error handling

- `put` never raises to the caller; malformed tasks are logged and dropped.
- Worker survives any exception from dispatch or callback (logged with
  traceback, matching `background_tasks_thread` style).
- Module resolution failure → logged error, `callback(False)`.

## Shutdown

On `KeyboardInterrupt`, `TWCManager.py` shuts down in this order, with the
whole sequence bounded at 80 s from the start of shutdown:

1. Queue `saveSettings` on the background queue first, so the background
   thread can work on it while the vehicle queue drains.
2. Call `master.vehicleCommandQueue.drain_high_priority(timeout=60)` (at most
   60 s).
3. Wait for the background queue to empty by polling its `unfinished_tasks`
   every 0.1 s, until it reaches 0 or the 80 s deadline passes. On the
   deadline, log a warning that background tasks were still pending at exit.
   (A background task such as `checkCharge` can be blocked on the BLE lock
   behind a retrying vehicle command, so an unbounded `join()` could run past
   systemd's SIGKILL.)
4. Close the interface module.

The vehicle command queue is **partly** drained: stop-charging commands still
run, everything else is dropped. `drain_high_priority`:

1. Under the lock, sets a `draining` flag and clears the low-priority table.
   While `draining` is set, `put` accepts only high-priority tasks; other
   tasks are logged at debug level and dropped (their callbacks are not
   called).
2. Waits on the condition until the high table is empty and the worker is
   not executing anything, or until `timeout` expires. If a low-priority
   command is already executing, the wait includes letting it finish,
   because there is no pre-emption.
3. On timeout, logs a warning naming the commands still pending and
   returns. The daemon worker is then killed at interpreter exit.

**systemd stop/restart.** Today nothing installs a SIGTERM handler. Under
systemd (`KillMode=control-group`, SIGKILL after `TimeoutStopSec`, 90 s by
default), Python dies on SIGTERM without unwinding, so neither the
background-queue drain nor `saveSettings` runs. This change installs a
SIGTERM handler, just before the main loop, that restores the default
SIGTERM disposition (so a second SIGTERM kills immediately) and raises
`KeyboardInterrupt` in the main thread. `systemctl stop` and `restart` then
take the same shutdown path as Ctrl-C. The total shutdown (60 s drain plus
the bounded background wait, under 80 s in all) fits inside systemd's 90 s
SIGKILL backstop. systemd also sends SIGTERM to any running
`tesla-control` child, so an in-flight BLE command dies; a stop command then
retries inside the drain window.

Rationale: a stop the user or policy asked for should still reach the car,
but rate and limit changes are meaningless once TWCManager stops managing
the charger, and shutdown should not wait minutes on BLE retries for them.
The 60 s cap bounds the drain even if a stop is retrying. A `charge`
False task that is still being retried can, in the worst case, take longer
than that.

## Behavior changes visible to users

- Charge-rate changes via BLE/API happen shortly after the decision rather
  than inline; logs for them appear interleaved with heartbeat logs.
- Background-thread work (green energy polling, VIN queries, status updates)
  no longer waits on BLE `charge` / `applyChargeLimit` commands.
- `background_tasks_thread`'s fallback when `VehiclePriority` is absent did
  not include `TeslaBLE`; the vehicle worker's resolution order does. In
  practice `VehiclePriority` is always in `modules_available`.
- Pending `charge` tasks are replaced, not field-merged, when coalesced. On
  main, a start queued after a pending stop-with-VIN inherited that VIN; now
  the start applies to all vehicles, as intended.
- A `charge` task put while another is executing now queues a fresh pending
  entry. On main it mutated the in-flight dict and was effectively lost.
- The raise-to-wiring-max path `int()`-truncates a fractional
  `wiringMaxAmpsPerTWC`.
- SIGTERM (`systemctl stop` / `restart`) now runs the graceful shutdown
  instead of killing the process outright.

## Testing

Unit tests (pytest, `tests/unit/`):

- `test_vehicle_command_queue.py`
  - dispatches each supported `cmd` to the right method with the right args;
  - coalescing: two pending `setChargeRate` for one VIN → one dispatch
    with the latest amps; different VINs → two dispatches;
  - pending vehicle-less start, then stop with a VIN → single stop dispatched;
  - priority: with a slow in-flight task, queued `[setChargeRate, charge
    start, charge stop]` → stop dispatched first after the in-flight task;
  - callback receives result; callback receives `False` on dispatch
    exception; callback exception does not kill worker;
  - `resolve_module` returning `None` → callback `False`, worker continues;
  - `put` returns immediately while worker is blocked in a slow dispatch;
  - `drain_high_priority`: pending stop runs, pending low-priority tasks are
    dropped and never dispatched; low-priority puts made while draining are
    dropped; it waits for an executing low-priority command to finish; it
    returns after `timeout` with a stuck dispatch.
- `test_twcslave.py` updates
  - heartbeat path enqueues `setChargeRate` via `queue_vehicle_command`
    instead of calling the vehicle module;
  - `vehicleRateRaised` set by callback on success, not on `False`;
  - stale callback after a switch to API control does not set
    `vehicleRateRaised`.
- `test_twcmaster.py` additions
  - `queue_background_task` routes `charge` / `applyChargeLimit` to the
    vehicle queue and other cmds to the background queue;
  - delayed `charge` task is routed when it matures.
- `test_teslable_lock.py`
  - two threads calling `_sendCommand_internal` against a stubbed
    `_run_command_with_timeout` that records entry/exit; assert no overlap;
  - same for `_fetch_state_json` vs `_sendCommand_internal`.
- Existing `test_vehicle_priority.py` passes unchanged.

### Live testing (`ssh twcmanager`)

The live box runs TWCManager as `twcmanager.service` (systemd, user
`mbishop`) from `~/TWCManager` with a venv. It is currently at `a7c84f4` and
uses `chargeStopMode` 1. Its journal already shows the contention bug:
overlapping retry sequences for one VIN (two "retrying in 4s (attempt 2/4)"
lines one second apart, 18:32:51 and 18:32:52 on 2026-10-07).

Procedure. Each deploy or restart is confirmed with the user first.

1. **Baseline.** Deploy only Component 4 (the stall warning) on top of
   the box's current code and restart. Collect for a period that includes
   BLE activity:
   - count and maximum of `Main loop stalled` warnings;
   - number of overlapping BLE retry sequences (more than one
     `_execute_with_retry` chain in flight for a VIN, visible as interleaved
     attempt counters);
   - `tesla-control` "no such file or directory" failures.
2. **Deploy the branch.** Record the box's prior commit for rollback, then
   restart.
3. **Measure the same metrics.** Expect:
   - no stall warnings attributable to BLE;
   - no overlapping BLE operations;
   - no "no such file or directory" key-file failures.
4. **Exercise the paths:**
   - plug in a car, or use the web UI start/stop, so `charge` commands run;
   - change policy to trigger `applyChargeLimit`;
   - let charge-rate control run so `setChargeRate` is queued from the
     heartbeat path;
   - while one BLE command is retrying, issue a stop and confirm from the
     logs that it runs next.
5. **Rollback:** `git checkout <prior>` in `~/TWCManager`, then
   `sudo systemctl restart twcmanager`.
