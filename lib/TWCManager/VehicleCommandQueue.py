# Serializes state-changing vehicle commands onto one worker thread.
#
# Vehicle commands (especially over BLE) can take minutes when they retry.
# The main loop must keep servicing the TWC serial link, so callers put()
# commands here and return immediately. The worker runs one command at a
# time against the vehicle module (normally VehiclePriority), which keeps
# its synchronous BLE -> Fleet API fallback.
#
# Pending commands coalesce: setChargeRate and charge per vehicle,
# applyChargeLimit per command. A charge command without a vin applies to
# every car, so it replaces, and is replaced by, any pending charge command.
# Stop-charging commands run before anything else.
#
# A car gets at most one charge start/stop attempt per
# CHARGE_COMMAND_INTERVAL. Callers re-request every loop while they want a
# change, so a refused command isn't retried back to back.
import collections
import threading
import time
import traceback

from TWCManager.Logging.LoggerFactory import LoggerFactory

logger = LoggerFactory.get_logger("VehicleCommandQueue", "Vehicle")


class VehicleCommandQueue:
    CHARGE_COMMAND_INTERVAL = 60

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
        # Per-VIN (None: every car) time of the last charge attempt
        self._lastChargeTime = {}

    @staticmethod
    def _key(task):
        cmd = task["cmd"]
        if cmd == "charge":
            # TWCSlave passes "" when it hasn't read the car's VIN
            return (cmd, task.get("vin") or None)
        if cmd != "setChargeRate":
            return (cmd, None)
        vehicle = task.get("vehicle")
        return (cmd, getattr(vehicle, "VIN", vehicle))

    @staticmethod
    def _replaces(key, other):
        # A charge command for every car (vin None) and one for a single car
        # overlap, so the newer one replaces the older.
        if key[0] != "charge" or other[0] != "charge":
            return key == other
        return key[1] is None or other[1] is None or key[1] == other[1]

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
                if key[0] == "charge" and not self._draining:
                    since = time.monotonic() - max(
                        (
                            t
                            for k, t in self._lastChargeTime.items()
                            if self._replaces(key, ("charge", k))
                        ),
                        default=float("-inf"),
                    )
                    if since < self.CHARGE_COMMAND_INTERVAL:
                        logger.debug(
                            "Not sending %r; last charge command %ds ago",
                            task,
                            since,
                        )
                        return
                table = self._high if high else self._low
                for pending in (self._high, self._low):
                    for k in [k for k in pending if self._replaces(key, k)]:
                        # The same key in the same table keeps its place
                        if not (pending is table and k == key):
                            del pending[k]
                table[key] = (task, callback)
                self._cond.notify_all()
        except Exception:
            logger.error("Failed to queue vehicle command: %s", traceback.format_exc())

    def cancel_stop(self, vin=None):
        # Drop pending stop-charging commands for this car, and any stop for
        # every car; with no vin, drop every pending stop. A stop already
        # running can't be cancelled. Not during shutdown, which must send
        # its stops.
        vin = vin or None
        with self._cond:
            if self._draining:
                return 0
            keys = [k for k in self._high if vin is None or k[1] is None or k[1] == vin]
            for k in keys:
                del self._high[k]
            if keys:
                logger.debug("Cancelled %d pending stop command(s)", len(keys))
            return len(keys)

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
            key, entry = table.popitem(last=False)
            if key[0] == "charge":
                self._lastChargeTime[key[1]] = time.monotonic()
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
        try:
            while True:
                task, callback = self._next()
                try:
                    result = self._execute(task)
                    if callback is not None:
                        try:
                            callback(result)
                        except Exception:
                            logger.error(
                                "Vehicle command callback failed: %s",
                                traceback.format_exc(),
                            )
                finally:
                    # Clear even on BaseException so drain_high_priority and
                    # waiters aren't left blocked on a dead worker.
                    with self._cond:
                        self._busy = False
                        self._cond.notify_all()
        except BaseException:
            logger.error(
                "Vehicle command worker thread %s exiting: %s",
                threading.current_thread().name,
                traceback.format_exc(),
            )
            raise
