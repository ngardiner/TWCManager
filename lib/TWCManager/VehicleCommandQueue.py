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
# reconciler's attempt queues (Vehicle/Reconcile/Reconciler.py) pass their
# own run, key, isHigh, report and lost. Every entry such an instance
# accepts is reported exactly once, through report or lost. Callbacks never
# run under _cond.
import collections
import threading
import time
import traceback

from TWCManager.Logging.LoggerFactory import LoggerFactory

logger = LoggerFactory.get_logger("VehicleCommandQueue", "Vehicle")


def _setAmps(module, task):
    setAmps = getattr(module, "setAmps", None)
    if setAmps is None:
        # VehiclePriority is not loaded; the fallback module has no setAmps
        logger.debug("No setAmps on the vehicle command module; dropping it")
        return False
    return setAmps(task["vin"], task["amps"])


class VehicleCommandQueue:
    MAX_PENDING = 64

    _DISPATCH = {
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
    ):
        self._resolve_module = resolve_module
        self.name = name
        self._legacy = run is None
        self._runEntry = self._execute if run is None else run
        self._keyOf = key or self._key
        # The legacy instance runs no stops: none is high
        self._isHigh = isHigh or (lambda entry: False)
        self._report = report
        self._lost = lost
        self._cond = threading.Condition()
        self._high = collections.OrderedDict()
        self._low = collections.OrderedDict()
        self._busy = False
        self._draining = False
        self._thread = None
        # The worker thread is between start() and its exit
        self._live = False
        self._overflowWarned = False

    @staticmethod
    def _key(task):
        cmd = task["cmd"]
        if cmd == "setChargeRate":
            vehicle = task.get("vehicle")
            return (cmd, getattr(vehicle, "VIN", vehicle))
        # None is a distinct target meaning "every vehicle".
        return (cmd, task.get("vin"))

    def start(self):
        with self._cond:
            if self._thread is not None or self._draining:
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
            dead = self._thread is not None and not self._live
            if dead or (self._draining and not (high and self._live)):
                # A dead or exited worker takes nothing; shutting down, only
                # a stop a worker can still run is taken
                losers.append((entry, "dropped"))
                accepted = False
            else:
                table, other = (
                    (self._high, self._low) if high else (self._low, self._high)
                )
                for t in (other, table):
                    if key in t:
                        losers.append((t[key][0], "replaced"))
                        if t is other:
                            del t[key]
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
            if self._thread is None:
                # Never started (startup died first): nothing will run them
                done = False
            elif not self._live:
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
            _, (entry, callback) = table.popitem(last=False)
            self._busy = True
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
            # left blocked on a dead worker
            with self._cond:
                self._busy = False
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
