# Serializes state-changing vehicle commands onto one worker thread.
#
# Vehicle commands (especially over BLE) can take minutes when they retry.
# The main loop must keep servicing the TWC serial link, so callers put()
# commands here and return immediately. The worker runs one entry at a time.
#
# Pending entries coalesce per key: a later entry with the same key replaces
# the pending one. High entries (stops) run before anything else. remove()
# lets a caller drop pending entries that a newer decision has made moot. At
# shutdown, close() drops the rest and waitDrained() lets the stops finish.
#
# Two kinds of instance: the legacy one (run=None) dispatches command dicts
# to the vehicle module (normally VehiclePriority) until Task 13; the
# attempt workers (Vehicle/Reconcile/Run.py) pass their own run, key, isHigh,
# report and lost. Every entry such an instance accepts is reported exactly
# once, through report or lost. Callbacks never run under _cond.
#
# Legacy charge commands: one without a vin applies to every car, so it
# replaces, and is replaced by, any pending charge command. A car gets at
# most one charge start/stop attempt per CHARGE_COMMAND_INTERVAL. Callers
# re-request every loop while they want a change, so a refused command isn't
# retried back to back.
import collections
import threading
import time
import traceback

from TWCManager.Logging.LoggerFactory import LoggerFactory

logger = LoggerFactory.get_logger("VehicleCommandQueue", "Vehicle")


def _applyChargeLimitKwargs(task):
    kwargs = {"limit": task["limit"]}
    # Pass vin and remoteOnly only when set, so a plain apply is unchanged for
    # any module that still has the old signature.
    if task.get("vin") is not None:
        kwargs["vin"] = task["vin"]
    if task.get("remoteOnly"):
        kwargs["remoteOnly"] = True
    return kwargs


def _setAmps(module, task):
    setAmps = getattr(module, "setAmps", None)
    if setAmps is None:
        # VehiclePriority is not loaded; the fallback module has no setAmps
        logger.debug("No setAmps on the vehicle command module; dropping it")
        return False
    return setAmps(task["vin"], task["amps"])


class VehicleCommandQueue:
    MAX_PENDING = 64
    CHARGE_COMMAND_INTERVAL = 60

    _DISPATCH = {
        "charge": lambda module, task: module.car_api_charge(task),
        "applyChargeLimit": lambda module, task: module.applyChargeLimit(
            **_applyChargeLimitKwargs(task)
        ),
        "setChargeRate": lambda module, task: module.setChargeRate(
            task["amps"], task.get("vehicle")
        ),
        "setAmps": lambda module, task: _setAmps(module, task),
    }

    def __init__(
        self,
        resolve_module=None,
        *,
        name="VehicleCommandQueue",
        run=None,
        key=None,
        isHigh=None,
        report=None,
        lost=None,
        inline=False,
    ):
        self._resolve_module = resolve_module
        self.name = name
        self._legacy = run is None
        self._runEntry = self._execute if run is None else run
        self._keyOf = key or self._key
        self._isHigh = isHigh or self._is_high_priority
        self._report = report
        self._lost = lost
        self.inline = inline
        self._cond = threading.Condition()
        self._high = collections.OrderedDict()
        self._low = collections.OrderedDict()
        self._busy = False
        self._running = None
        self._draining = False
        self._thread = None
        # The worker thread is between start() and its exit
        self._live = False
        self._overflowWarned = False
        # Legacy: per-VIN (None: every car) time of the last charge attempt
        self._lastChargeTime = {}

    @staticmethod
    def _key(task):
        cmd = task["cmd"]
        if cmd == "setChargeRate":
            vehicle = task.get("vehicle")
            return (cmd, getattr(vehicle, "VIN", vehicle))
        if cmd == "charge":
            # TWCSlave passes "" when it hasn't read the car's VIN
            return (cmd, task.get("vin") or None)
        # None is a distinct target meaning "every vehicle".
        return (cmd, task.get("vin"))

    @staticmethod
    def _replaces(key, other):
        # A legacy charge command for every car (vin None) and one for a
        # single car overlap, so the newer one replaces the older.
        if key[0] != "charge" or other[0] != "charge":
            return key == other
        return key[1] is None or other[1] is None or key[1] == other[1]

    def _chargeHeld(self, key):
        # Legacy: True while this charge command's car (or any car, for a
        # vehicle-less command) had a charge attempt within the interval
        since = time.monotonic() - max(
            (
                t
                for k, t in self._lastChargeTime.items()
                if self._replaces(key, ("charge", k))
            ),
            default=float("-inf"),
        )
        return since < self.CHARGE_COMMAND_INTERVAL

    @staticmethod
    def _is_high_priority(task):
        return task["cmd"] == "charge" and "charge" in task and not task["charge"]

    def start(self):
        with self._cond:
            if self._thread is not None or self.inline or self._draining:
                return
            self._thread = threading.Thread(
                target=self._run, name=self.name, daemon=True
            )
            self._live = True
        self._thread.start()

    # --- reporting (never under _cond) ---

    def _lose(self, losers):
        for entry, detail in losers:
            if self._lost is None:
                logger.debug("%s: %s vehicle command dropped", self.name, detail)
                continue
            try:
                self._lost(entry, detail)
            except Exception as e:
                self._logFailure("lost callback", e)

    def _logFailure(self, what, e):
        # The type name only at WARNING; the traceback, which may carry
        # vehicle data, at DEBUG
        logger.warning("%s %s raised %s", self.name, what, type(e).__name__)
        logger.debug("%s %s traceback: %s", self.name, what, traceback.format_exc())

    def _deliver(self, entry, callback, result):
        for fn, args in ((callback, (result,)), (self._report, (entry, result))):
            if fn is None:
                continue
            try:
                fn(*args)
            except Exception as e:
                self._logFailure("callback", e)

    # --- the queue ---

    def put(self, entry, callback=None) -> bool:
        if self._legacy and not (isinstance(entry, dict) and "cmd" in entry):
            logger.error("Dropping malformed vehicle command: %r", entry)
            return False
        try:
            key, high = self._keyOf(entry), bool(self._isHigh(entry))
        except Exception as e:
            self._logFailure("key", e)
            return False
        losers, accepted, warn = [], True, False
        with self._cond:
            dead = not self.inline and self._thread is not None and not self._live
            if dead or (self._draining and not (high and (self.inline or self._live))):
                # A dead or exited worker takes nothing; shutting down, only
                # a stop a worker can still run is taken
                losers.append((entry, "dropped"))
                accepted = False
            elif (
                self._legacy
                and key[0] == "charge"
                and not self._draining
                and self._chargeHeld(key)
            ):
                logger.debug("Not sending %r; a charge command went recently", entry)
                losers.append((entry, "dropped"))
                accepted = False
            elif self.inline:
                self._running, self._busy = entry, True
            else:
                table, other = (
                    (self._high, self._low) if high else (self._low, self._high)
                )
                replaces = self._replaces if self._legacy else (lambda a, b: a == b)
                for t in (other, table):
                    for k in [k for k in t if replaces(key, k)]:
                        losers.append((t[k][0], "replaced"))
                        if t is other or k != key:
                            del t[k]
                if (
                    key not in table
                    and len(self._high) + len(self._low) >= self.MAX_PENDING
                ):
                    warn = not self._overflowWarned
                    self._overflowWarned = True
                    if self._low:
                        losers.append((self._low.popitem(last=False)[1][0], "dropped"))
                    else:
                        # A queue full of stops keeps them: the newcomer loses
                        losers.append((entry, "dropped"))
                        accepted = False
                if accepted:
                    # A replacement keeps its place in the queue
                    table[key] = (entry, callback)
                    self._cond.notify_all()
        if warn:
            logger.warning(
                "%s is full (%d pending): dropping the oldest commands",
                self.name,
                self.MAX_PENDING,
            )
        self._lose(losers)
        if accepted and self.inline:
            self._runOne(entry, callback)
        return accepted

    def remove(self, match) -> int:
        # Pending (not running) entries for which match is true. The running
        # entry is left alone: it may already be talking to the car.
        losers = []
        with self._cond:
            for table in (self._high, self._low):
                for key in [k for k, (e, _) in table.items() if match(e)]:
                    losers.append((table.pop(key)[0], "cancelled"))
        if losers:
            logger.debug("%s: cancelled %d pending command(s)", self.name, len(losers))
        self._lose(losers)
        return len(losers)

    def cancel(self, cmd, match):
        # Legacy: until Task 13. Stops are kept during shutdown, which must
        # send them.
        return self.remove(
            lambda task: task["cmd"] == cmd
            and match(task)
            and not (self._draining and self._is_high_priority(task))
        )

    def pending(self) -> int:
        with self._cond:
            return len(self._high) + len(self._low)

    @property
    def busy(self) -> bool:
        with self._cond:
            return self._busy

    def _entries(self):
        # Test-only snapshot, high then low
        with self._cond:
            return [e for e, _ in list(self._high.values()) + list(self._low.values())]

    def summary(self) -> dict:
        with self._cond:
            running = self._running
            if running is not None and hasattr(running, "_asdict"):
                running = running._asdict()
            return {
                "pending": len(self._high) + len(self._low),
                "busy": self._busy,
                "running": running,
                "started": self._thread is not None,
                "closed": self._draining,
            }

    # --- shutdown ---

    def close(self) -> None:
        # No new non-high entries; every pending non-high entry is dropped
        with self._cond:
            self._draining = True
            losers = [(e, "dropped") for e, _ in self._low.values()]
            self._low.clear()
            self._cond.notify_all()
        if losers:
            logger.info(
                "%s: shutting down; dropping %d pending vehicle command(s)",
                self.name,
                len(losers),
            )
        self._lose(losers)

    def waitDrained(self, deadline) -> bool:
        # deadline is a time.monotonic stamp. Waits for the pending high
        # entries and the running one; on timeout, what has not started
        # never will
        with self._cond:
            if self._thread is None and not self.inline:
                # Never started (startup died first): nothing will run them
                done = False
            elif not self.inline and not self._live:
                # The worker has exited or died: there is nothing to wait for
                done = not self._high and not self._busy
            else:
                done = self._cond.wait_for(
                    lambda: not self._high and not self._busy,
                    max(0.0, deadline - time.monotonic()),
                )
            losers = []
            if not done and (self._lost is not None or not self._live):
                losers = [(e, "dropped") for e, _ in self._high.values()]
                self._high.clear()
            elif not done:
                logger.warning(
                    "Shutting down with vehicle commands still pending: %r",
                    [task for task, _ in self._high.values()],
                )
        if losers:
            logger.info(
                "%s: drain timed out with %d stop(s) pending", self.name, len(losers)
            )
        self._lose(losers)
        return done

    def drain_high_priority(self, timeout):
        self.close()
        return self.waitDrained(time.monotonic() + timeout)

    # --- running ---

    def _next(self):
        with self._cond:
            self._cond.wait_for(lambda: self._high or self._low or self._draining)
            if not self._high and not self._low:
                # Drained: nothing left to run. Not live from here on, under
                # the same lock, so a racing put is rejected, never orphaned
                self._live = False
                self._cond.notify_all()
                return None
            table = self._high if self._high else self._low
            key, (entry, callback) = table.popitem(last=False)
            if self._legacy and key[0] == "charge":
                self._lastChargeTime[key[1]] = time.monotonic()
            self._running, self._busy = entry, True
            if not self._high and not self._low:
                # The backlog has cleared: the next overflow warns again
                self._overflowWarned = False
            return entry, callback

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

    def _runOne(self, entry, callback):
        # The callback and report run before busy clears, so a drain that
        # sees the queue idle has seen every report
        try:
            try:
                result = self._runEntry(entry)
            except BaseException as e:
                logger.error("%s: run raised %s", self.name, type(e).__name__)
                self._deliver(entry, None, e)
                if not isinstance(e, Exception):
                    raise
            else:
                self._deliver(entry, callback, result)
        finally:
            # Clear even on BaseException so waitDrained and waiters aren't
            # left blocked on a dead worker. Inline callers may overlap: busy
            # shows the latest one running
            with self._cond:
                if self._running is entry:
                    self._running = None
                self._busy = self._running is not None
                self._cond.notify_all()

    def _run(self):
        try:
            while True:
                item = self._next()
                if item is None:
                    return
                self._runOne(*item)
        except BaseException:
            logger.error(
                "Vehicle command worker thread %s exiting: %s",
                threading.current_thread().name,
                traceback.format_exc(),
            )
            raise
        finally:
            with self._cond:
                self._live = False
                self._cond.notify_all()
