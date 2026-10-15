# Lanes: per-channel attempt runners. The ble and cloud lanes each run one
# thread that pops an attempt, runs it (read before write, wake first, the
# command, the own-write stamp) and reports its outcome; the local lane runs
# inline on the caller's thread, since its providers only publish a request
# that the serial thread's heartbeat sends. runAttempt is the one place a
# vehicle command is issued.
#
# Every entry a lane accepts is reported exactly once: run, replaced,
# dropped, cancelled or drained. Reports carry the submitted Attempt object
# itself, so a caller can match them by identity. Lanes never touch
# AttemptRecord or ChannelHealth; the report callback runs on the lane's
# thread (or the submitter's, inline) and does that.
#
# One clock domain: attempts carry time.time stamps (the registry's). Only
# the rate-limit window uses time.monotonic. No lock is held while running
# an attempt or calling the report callback.
import itertools
import logging
import threading
import time
from collections import deque
from typing import Callable, Dict, FrozenSet, List, Optional, Tuple

from ..Providers.Capability import (
    SET_AMPS,
    SET_CHARGE_LIMIT,
    STOP_CHARGING,
    TIER_DISRUPTIVE,
    WAKE,
)
from ..Providers.Provider import PROBE, commandMethod
from .Desired import AMPS, CHARGE_LIMIT, CHARGING, FIELD_FOR, Attempt
from .Outcome import (
    DONE,
    ERROR,
    REFUSED,
    SATISFIED,
    UNREACHABLE,
    UNSUPPORTED,
    Outcome,
    isOutcome,
)

logger = logging.getLogger(__name__.split(".")[0] + ".Vehicle.Reconcile")

# The rate-limit window's clock, the one use of monotonic time here (an alias
# a test can patch without touching the time module)
_monotonic = time.monotonic

LANE_PROVIDERS = {
    "local": frozenset({"Gen2TWC", "Gen3TWC"}),
    "ble": frozenset({"TeslaBLE"}),
    "cloud": frozenset(
        {
            "OwnerAPI",
            "FleetAPI",
            "HomeAssistant",
            "TeslaMateVehicle",
            "FleetTelemetryMQTT",
        }
    ),
}

# Commands that take the attempt's value after the vin
_VALUE_COMMANDS = frozenset({SET_CHARGE_LIMIT, SET_AMPS})
# A provider's SATISFIED is stamped for these only: a SATISFIED start/stop
# leaves the state the read gave
_STAMP_SATISFIED = frozenset({CHARGE_LIMIT, AMPS})
# The wake outcomes after which a non-local provider's command goes out
_WAKE_PROCEEDS = frozenset({DONE, SATISFIED, UNSUPPORTED})

_RECONCILER = __name__.rsplit(".", 1)[0] + ".Reconciler"

# One-shot entry keys: a fresh number per one-shot, so none replaces another
_sequence = itertools.count(1)


def entryKey(attempt) -> tuple:
    if attempt.aspect is not None:
        return (attempt.key, attempt.aspect)
    if attempt.capability == PROBE:
        return (attempt.key, PROBE)
    return (attempt.key, "debug", next(_sequence))


def _isStop(attempt) -> bool:
    return attempt.capability == STOP_CHARGING


def _isOneShot(attempt) -> bool:
    return attempt.aspect is None and attempt.capability != PROBE


# Placeholders until Task 6's Reconciler provides the real rules
def _satisfied(aspect, desired, believed) -> bool:
    return believed is not None and believed.value == desired


_VEHICLE_SOURCES_EXCLUDED = frozenset()


def _reconcilerParts():
    # Lazy: Reconciler imports this module. Only a missing Reconciler module
    # falls back; any other import failure is a real error and propagates
    try:
        from .Reconciler import VEHICLE_SOURCES_EXCLUDED, VehicleReconciler, satisfied
    except ModuleNotFoundError as e:
        if e.name != _RECONCILER:
            raise
        return None, _satisfied, _VEHICLE_SOURCES_EXCLUDED
    return VehicleReconciler, satisfied, VEHICLE_SOURCES_EXCLUDED


def writtenValue(aspect, value):
    # What the own-write stamp records, in the field's own vocabulary
    if aspect == CHARGING:
        return "Charging" if value is True else "Stopped"
    return value


def _call(provider, capability, vin, value):
    method = getattr(provider, commandMethod(capability))
    if capability in _VALUE_COMMANDS:
        return method(vin, value)
    return method(vin)


def runAttempt(master, lane, attempt) -> Outcome:
    vin = attempt.key
    vehicle = master.vehicleRegistry.get(vin)
    if vehicle is None:
        return Outcome(ERROR, "no vehicle")
    provider = master.providerOrder.provider(attempt.provider)
    if provider is None:
        return Outcome(ERROR, "no provider")
    try:
        lane.note(provider, attempt.capability, vin)
        if attempt.capability == PROBE:
            return lane._checked(provider, "probe", provider.probe(vin))
        reconcilerType, satisfied, excluded = _reconcilerParts()
        hooks = getattr(master, "reconciler", None)
        if reconcilerType is None or not isinstance(hooks, reconcilerType):
            hooks = None
        aspect = attempt.aspect
        field = FIELD_FOR[aspect] if aspect is not None else None
        if field is not None and attempt.maxAge is not None:
            sources = [
                s
                for s in master.vehicleRegistry.readSources()
                if getattr(s, "provider", "") in lane.providers
            ]
            vehicle.read(
                field,
                attempt.maxAge,
                allowWake=False,
                sources=sources,
                force=attempt.force,
            )
            believed = vehicle.believed(field, exclude=excluded)
            if satisfied(aspect, attempt.value, believed):
                return Outcome(SATISFIED, "read")
        else:
            believed = (
                vehicle.believed(field, exclude=excluded) if field is not None else None
            )
        if hooks is not None:
            value, early = hooks.beforeWrite(vehicle, attempt, provider, believed)
            if early is not None:
                return early
        else:
            value = attempt.value
        if attempt.wake and provider.declares(WAKE):
            woke = lane._checked(provider, WAKE, provider.wake(vin))
            if woke.kind == REFUSED:
                return woke
            if woke.kind not in _WAKE_PROCEEDS:
                if not provider.local:
                    # A billed command to a car that may still be asleep is
                    # never sent: any failed wake ends the attempt
                    return woke
                # Today's BLE behaviour: the command still goes out
                logger.debug(
                    "%s wake for %s failed (%s); sending anyway",
                    provider.name,
                    vin,
                    woke.kind,
                )
        outcome = lane._checked(
            provider,
            commandMethod(attempt.capability),
            _call(provider, attempt.capability, vin, value),
        )
    except Exception as e:
        logger.debug(
            "%s %s for %s raised %s",
            attempt.provider,
            attempt.capability,
            vin,
            type(e).__name__,
        )
        return Outcome(ERROR, type(e).__name__)
    # The command has run: whatever fails from here on, its outcome stands. A
    # paid command that took effect must never come back as a retryable ERROR
    try:
        if aspect is not None and (
            outcome.kind == DONE
            or (outcome.kind == SATISFIED and aspect in _STAMP_SATISFIED)
        ):
            cost = provider.cost(attempt.capability)
            if cost is None or cost.tier != TIER_DISRUPTIVE:
                vehicle.noteWrite(field, writtenValue(aspect, value), provider.name)
    except Exception as e:
        logger.warning(
            "Lane %s: stamping %s for %s raised %s",
            lane.name,
            attempt.capability,
            vin,
            type(e).__name__,
        )
    if hooks is not None:
        try:
            hooks.afterWrite(vehicle, attempt, outcome)
        except Exception as e:
            logger.warning(
                "Lane %s: afterWrite for %s %s raised %s",
                lane.name,
                attempt.capability,
                vin,
                type(e).__name__,
            )
    return outcome


class Lane:
    MAX_PENDING = 64
    # The bounded wait between checks for work or a stop
    WAIT = 1.0

    def __init__(
        self,
        name,
        providers: FrozenSet[str],
        master,
        report: Callable[[Attempt, Outcome], None],
        clock=time.time,
        inline=False,
    ):
        self.name = name
        self.providers = frozenset(providers)
        self.master = master
        self._report = report
        # The domain the attempts carry; the lane stamps nothing with it
        self.clock = clock
        self.inline = inline
        self._cond = threading.Condition()
        # (entryKey, attempt), stops first, FIFO within each group
        self._pending: List[Tuple[tuple, Attempt]] = []
        self._running: Optional[Attempt] = None
        self._stopping = False
        # No new entries once a drain or stop has begun
        self._closed = False
        self._thread: Optional[threading.Thread] = None
        self._overflowWarned = False
        # (provider, capability, vin) -> (monotonic stamps, perSeconds)
        self._windows: Dict[tuple, Tuple[deque, float]] = {}
        # (provider, method) already warned about a bad return
        self._badReturns = set()

    # --- reporting ---

    def _deliver(self, attempt, outcome) -> None:
        if _isOneShot(attempt):
            logger.info(
                "Debug %s for %s via %s: %s %s",
                attempt.capability,
                attempt.key,
                attempt.provider,
                outcome.kind,
                outcome.detail,
            )
        else:
            logger.debug(
                "Lane %s: %s %s for %s via %s: %s %s",
                self.name,
                attempt.aspect,
                attempt.capability,
                attempt.key,
                attempt.provider,
                outcome.kind,
                outcome.detail,
            )
        try:
            self._report(attempt, outcome)
        except Exception as e:
            logger.warning(
                "Lane %s report callback raised %s", self.name, type(e).__name__
            )

    def _deliverAll(self, losers) -> None:
        for attempt, detail in losers:
            self._deliver(attempt, Outcome(ERROR, detail))

    def _checked(self, provider, method, result) -> Outcome:
        # The provider return contract
        if isOutcome(result):
            return result
        kind = type(result).__name__
        with self._cond:
            first = (provider.name, method) not in self._badReturns
            self._badReturns.add((provider.name, method))
        if first:
            logger.warning(
                "%s %s: bad return %s, not an Outcome", provider.name, method, kind
            )
        return Outcome(ERROR, "bad return " + kind)

    def _runOne(self, attempt) -> None:
        try:
            outcome = runAttempt(self.master, self, attempt)
        except Exception as e:
            # Never kills the thread: the entry is still reported
            outcome = Outcome(ERROR, type(e).__name__)
        if not isOutcome(outcome):
            outcome = Outcome(ERROR, "bad return " + type(outcome).__name__)
        self._deliver(attempt, outcome)

    # --- the queue ---

    def submit(self, attempt) -> bool:
        if self.inline:
            with self._cond:
                closed = self._closed
            if closed:
                self._deliver(attempt, Outcome(ERROR, "dropped"))
                return False
            # Inline callers may overlap; busy shows the latest one running
            with self._cond:
                self._running = attempt
            try:
                self._runOne(attempt)
            finally:
                with self._cond:
                    if self._running is attempt:
                        self._running = None
            return True
        key = entryKey(attempt)
        losers = []
        accepted = True
        warn = False
        with self._cond:
            if self._closed:
                losers.append((attempt, "dropped"))
                accepted = False
            else:
                for i, (k, old) in enumerate(self._pending):
                    if k == key:
                        del self._pending[i]
                        losers.append((old, "replaced"))
                        break
                if len(self._pending) >= self.MAX_PENDING:
                    victim = next(
                        (i for i, (_, a) in enumerate(self._pending) if not _isStop(a)),
                        None,
                    )
                    if victim is None:
                        # A lane full of stops keeps them: the newcomer loses
                        losers.append((attempt, "dropped"))
                        accepted = False
                    else:
                        losers.append((self._pending.pop(victim)[1], "dropped"))
                    warn = not self._overflowWarned
                    self._overflowWarned = True
                if accepted:
                    if _isStop(attempt):
                        at = next(
                            (
                                i
                                for i, (_, a) in enumerate(self._pending)
                                if not _isStop(a)
                            ),
                            len(self._pending),
                        )
                        self._pending.insert(at, (key, attempt))
                    else:
                        self._pending.append((key, attempt))
                    self._cond.notify_all()
        if warn:
            logger.warning(
                "Lane %s is full (%d pending): dropping the oldest attempts",
                self.name,
                self.MAX_PENDING,
            )
        elif any(d == "dropped" for _, d in losers):
            logger.debug("Lane %s dropped an attempt", self.name)
        self._deliverAll(losers)
        return accepted

    def pending(self) -> int:
        with self._cond:
            return len(self._pending)

    @property
    def busy(self) -> bool:
        with self._cond:
            return self._running is not None

    def _peek(self) -> Optional[Attempt]:
        # Test-only snapshot
        with self._cond:
            return self._pending[0][1] if self._pending else None

    def _entries(self) -> List[Attempt]:
        # Test-only snapshot
        with self._cond:
            return [a for _, a in self._pending]

    def cancel(self, key) -> int:
        # Pending stops for key only; a running attempt is left alone
        with self._cond:
            keep, losers = [], []
            for entry in self._pending:
                attempt = entry[1]
                if attempt.key == key and _isStop(attempt):
                    losers.append((attempt, "cancelled"))
                else:
                    keep.append(entry)
            self._pending = keep
        self._deliverAll(losers)
        return len(losers)

    # --- rate limits ---

    def _rateLimit(self, provider, capability):
        try:
            declaration = provider.declarations().get(capability)
        except Exception as e:
            logger.debug("%s declarations() raised %s", provider.name, type(e).__name__)
            return None
        return declaration.rateLimit if declaration else None

    def allows(self, provider, capability, vin) -> bool:
        limit = self._rateLimit(provider, capability)
        if limit is None:
            return True
        now = _monotonic()
        with self._cond:
            window = self._windows.get((provider.name, capability, vin))
            if window is None:
                return True
            stamps = window[0]
            recent = sum(1 for t in stamps if now - t < limit.perSeconds)
            return recent < limit.calls

    def note(self, provider, capability, vin) -> None:
        limit = self._rateLimit(provider, capability)
        if limit is None or limit.calls < 1:
            return
        now = _monotonic()
        with self._cond:
            # Forget windows that no longer constrain anything, so the map
            # stays bounded by the cars recently commanded
            for k, (stamps, per) in list(self._windows.items()):
                if not stamps or now - stamps[-1] >= per:
                    del self._windows[k]
            key = (provider.name, capability, vin)
            window = self._windows.get(key)
            if window is None or window[0].maxlen != limit.calls:
                window = (deque(maxlen=limit.calls), limit.perSeconds)
                self._windows[key] = window
            window[0].append(now)

    # --- the thread ---

    def start(self) -> None:
        if self.inline:
            return
        with self._cond:
            if self._thread is not None or self._closed:
                return
            self._thread = threading.Thread(
                target=self._run, name="Lane-" + self.name, daemon=True
            )
            thread = self._thread
        thread.start()

    def _run(self) -> None:
        while not self._stopping:
            with self._cond:
                if not self._pending:
                    if self._closed:
                        # Drained: nothing left to run
                        self._stopping = True
                        self._cond.notify_all()
                        break
                    self._cond.wait(timeout=self.WAIT)
                    continue
                attempt = self._pending.pop(0)[1]
                self._running = attempt
                if not self._pending:
                    # The backlog has cleared: the next overflow warns again
                    self._overflowWarned = False
            try:
                self._runOne(attempt)
            finally:
                with self._cond:
                    self._running = None
                    self._cond.notify_all()

    def stop(self) -> None:
        # Abrupt: everything pending is dropped; the running attempt finishes
        with self._cond:
            self._closed = True
            self._stopping = True
            losers = [(a, "dropped") for _, a in self._pending]
            self._pending = []
            self._cond.notify_all()
        self._deliverAll(losers)

    def _beginDrain(self) -> None:
        # Close the lane and drop every pending non-stop
        with self._cond:
            self._closed = True
            keep, losers = [], []
            for entry in self._pending:
                if _isStop(entry[1]):
                    keep.append(entry)
                else:
                    losers.append((entry[1], "dropped"))
            self._pending = keep
            self._cond.notify_all()
        if losers:
            logger.info(
                "Lane %s drain dropped %d pending attempts", self.name, len(losers)
            )
        self._deliverAll(losers)

    def _finishDrain(self, deadline) -> bool:
        # deadline is a time.time stamp
        with self._cond:
            thread = self._thread
        if thread is None:
            # Never started: the pending stops run here, one by one, while
            # time remains (bounded by MAX_PENDING entries). Concurrent
            # drainers share one runner: while another runs, this one waits
            while time.time() < deadline:
                with self._cond:
                    if self._running is not None:
                        self._cond.wait(timeout=min(deadline - time.time(), self.WAIT))
                        continue
                    if not self._pending:
                        break
                    attempt = self._pending.pop(0)[1]
                    self._running = attempt
                try:
                    self._runOne(attempt)
                finally:
                    with self._cond:
                        self._running = None
                        self._cond.notify_all()
        else:
            with self._cond:
                while self._pending or self._running is not None:
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        break
                    self._cond.wait(timeout=min(remaining, self.WAIT))
        with self._cond:
            finished = not self._pending and self._running is None
            losers = []
            if not finished:
                # Out of time: what has not started never will
                losers = [(a, "dropped") for _, a in self._pending]
                self._pending = []
            self._stopping = True
            self._cond.notify_all()
        if losers:
            logger.info(
                "Lane %s drain timed out with %d stops pending",
                self.name,
                len(losers),
            )
        self._deliverAll(losers)
        if finished and thread is not None:
            thread.join(min(self.WAIT, max(0.1, deadline - time.time())))
        return finished

    def drain(self, timeout) -> bool:
        deadline = time.time() + max(0.0, timeout)
        self._beginDrain()
        return self._finishDrain(deadline)

    def summary(self) -> dict:
        with self._cond:
            running = self._running
            return {
                "inline": self.inline,
                "pending": len(self._pending),
                "busy": running is not None,
                "running": (
                    None
                    if running is None
                    else {
                        "key": running.key,
                        "capability": running.capability,
                        "provider": running.provider,
                    }
                ),
                "started": self._thread is not None,
                "closed": self._closed,
            }


class Lanes:
    def __init__(self, master, report=None, clock=time.time, inline=False):
        self.master = master
        self._callback = report
        self.clock = clock
        self._lanes = {
            name: Lane(
                name,
                providers,
                master,
                self._report,
                clock=clock,
                # The local providers only publish a request for the
                # heartbeat: no I/O, so they run on the caller's thread
                inline=True if name == "local" else inline,
            )
            for name, providers in LANE_PROVIDERS.items()
        }

    def _report(self, attempt, outcome) -> None:
        callback = self._callback
        if callback is not None:
            callback(attempt, outcome)

    def setReport(self, callback) -> None:
        self._callback = callback

    def laneFor(self, providerName) -> Lane:
        for name in ("local", "ble"):
            if providerName in self._lanes[name].providers:
                return self._lanes[name]
        return self._lanes["cloud"]

    def submit(self, attempt) -> bool:
        return self.laneFor(attempt.provider).submit(attempt)

    def allows(self, provider, capability, vin) -> bool:
        return self.laneFor(provider.name).allows(provider, capability, vin)

    def cancel(self, key) -> int:
        return sum(lane.cancel(key) for lane in self._lanes.values())

    def start(self) -> None:
        for lane in self._lanes.values():
            lane.start()

    def drain(self, timeout) -> bool:
        # One deadline for both: SIGTERM is never held past timeout
        deadline = time.time() + max(0.0, timeout)
        lanes = [self._lanes["cloud"], self._lanes["ble"]]
        for lane in lanes:
            lane._beginDrain()
        finished = True
        for lane in lanes:
            finished = lane._finishDrain(deadline) and finished
        return finished

    def pending(self) -> int:
        return sum(lane.pending() for lane in self._lanes.values())

    def summary(self) -> dict:
        return {name: lane.summary() for name, lane in self._lanes.items()}
