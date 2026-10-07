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
