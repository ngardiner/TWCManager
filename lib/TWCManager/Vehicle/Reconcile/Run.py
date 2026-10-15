# Run: one vehicle attempt (read before write, wake first, the command, the
# own-write stamp), the rate-limit windows, and the two workers that run
# attempts. The ble and cloud workers are VehicleCommandQueue instances, each
# with its own thread; a local provider (Gen2TWC, Gen3TWC) only publishes a
# request that the serial thread's heartbeat sends, so its attempts run
# inline on the caller's thread. runAttempt is the one place a vehicle command
# is issued.
#
# Every attempt a worker accepts is reported exactly once: run, replaced,
# dropped or cancelled. Reports carry the submitted Attempt object itself, so
# a caller can match them by identity. The report callback runs on the
# worker's thread for a run; on the caller's thread for a replaced, dropped
# or cancelled entry and for every inline or local attempt, where the whole
# of runAttempt (DesiredStore.get, the reconciler's beforeWrite) runs too. So
# never call Workers.submit or cancel while holding DesiredStore's lock or any
# lock the report handler takes: it would deadlock (for a TWC, on the serial
# thread).
#
# One clock domain: attempts carry time.time stamps (the registry's). Only
# the rate-limit windows use time.monotonic. No lock is held while running
# an attempt or calling the report callback.
import itertools
import logging
import threading
import time
from collections import deque

from ...VehicleCommandQueue import VehicleCommandQueue
from ..Providers.Capability import (
    SET_AMPS,
    SET_CHARGE_LIMIT,
    STOP_CHARGING,
    TIER_DISRUPTIVE,
    WAKE,
)
from ..Providers.Provider import commandMethod
from .Desired import AMPS, CHARGE_LIMIT, CHARGING, FIELD_FOR, DesiredStore
from .Outcome import (
    DONE,
    ERROR,
    REFUSED,
    SATISFIED,
    UNSUPPORTED,
    Outcome,
    isOutcome,
)

logger = logging.getLogger(__name__.split(".")[0] + ".Vehicle.Reconcile")

# The rate-limit window's clock, the one use of monotonic time here (an alias
# a test can patch without touching the time module)
_monotonic = time.monotonic

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

# (provider, method) already warned about a bad return; bounded by the
# registered providers and their methods
_badReturns = set()
_badLock = threading.Lock()


def entryKey(attempt) -> tuple:
    if attempt.aspect is not None:
        return (attempt.key, attempt.aspect)
    return (attempt.key, "debug", next(_sequence))


def isStop(attempt) -> bool:
    return attempt.capability == STOP_CHARGING


# Placeholders until the Reconciler provides the real rules
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


def checked(provider, method, result) -> Outcome:
    # The provider return contract
    if isOutcome(result):
        return result
    kind = type(result).__name__
    with _badLock:
        first = (provider.name, method) not in _badReturns
        _badReturns.add((provider.name, method))
    if first:
        logger.warning(
            "%s %s: bad return %s, not an Outcome", provider.name, method, kind
        )
    return Outcome(ERROR, "bad return " + kind)


def _tagged(outcome, prefix) -> Outcome:
    if outcome.detail.startswith(prefix.strip()):
        return outcome
    return outcome._replace(detail=(prefix + outcome.detail).rstrip())


def _workerOf(master, providerName) -> str:
    # An unregistered provider counts as cloud
    provider = master.providerOrder.provider(providerName)
    return "cloud" if provider is None else getattr(provider, "worker", "cloud")


def _call(provider, capability, vin, value):
    method = getattr(provider, commandMethod(capability))
    if capability in _VALUE_COMMANDS:
        return method(vin, value)
    return method(vin)


class RateLimits:
    # Per (provider, aspect or capability, vin): HA's start and stop share
    # one CHARGING window. A slot is used only when a command is sent
    def __init__(self):
        # A leaf lock: nothing else is taken or called under it
        self._lock = threading.Lock()
        # key -> (monotonic stamps, perSeconds)
        self._windows = {}

    @staticmethod
    def _limit(provider, capability):
        try:
            declaration = provider.declarations().get(capability)
        except Exception as e:
            logger.debug("%s declarations() raised %s", provider.name, type(e).__name__)
            return None
        return declaration.rateLimit if declaration else None

    def allows(self, provider, capability, vin, aspect=None) -> bool:
        limit = self._limit(provider, capability)
        if limit is None:
            return True
        now = _monotonic()
        with self._lock:
            window = self._windows.get((provider.name, aspect or capability, vin))
            if window is None:
                return True
            recent = sum(1 for t in window[0] if now - t < limit.perSeconds)
            return recent < limit.calls

    def note(self, provider, capability, vin, aspect=None) -> None:
        limit = self._limit(provider, capability)
        if limit is None or limit.calls < 1:
            return
        now = _monotonic()
        with self._lock:
            # Forget windows that no longer constrain anything, so the map
            # stays bounded by the cars recently commanded
            for k, (stamps, per) in list(self._windows.items()):
                if not stamps or now - stamps[-1] >= per:
                    del self._windows[k]
            key = (provider.name, aspect or capability, vin)
            window = self._windows.get(key)
            if window is None or window[0].maxlen != limit.calls:
                window = (deque(maxlen=limit.calls), limit.perSeconds)
                self._windows[key] = window
            window[0].append(now)


def runAttempt(master, attempt, rateLimits) -> Outcome:
    vin = attempt.key
    aspect = attempt.aspect
    desired = getattr(master, "desired", None)
    if aspect is not None and not isStop(attempt) and isinstance(desired, DesiredStore):
        # The desire this attempt was made for has changed or gone while it
        # waited: a loser, with no read and no request
        want = desired.get(vin, aspect)
        if want is None or want.value != attempt.value:
            return Outcome(ERROR, "cancelled")
    vehicle = master.vehicleRegistry.get(vin)
    if vehicle is None:
        return Outcome(ERROR, "no vehicle")
    provider = master.providerOrder.provider(attempt.provider)
    if provider is None:
        return Outcome(ERROR, "no provider")
    try:
        reconcilerType, satisfied, excluded = _reconcilerParts()
        hooks = getattr(master, "reconciler", None)
        if reconcilerType is None or not isinstance(hooks, reconcilerType):
            hooks = None
        field = FIELD_FOR[aspect] if aspect is not None else None
        if field is not None and attempt.maxAge is not None:
            # Only the sources on this provider's own worker
            worker = getattr(provider, "worker", "cloud")
            sources = [
                s
                for s in master.vehicleRegistry.readSources()
                if _workerOf(master, getattr(s, "provider", "")) == worker
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
                return _tagged(early, "rules: ")
        else:
            value = attempt.value
        if attempt.wake and provider.declares(WAKE):
            woke = checked(provider, WAKE, provider.wake(vin))
            if woke.kind == REFUSED:
                return _tagged(woke, "wake ")
            if woke.kind not in _WAKE_PROCEEDS:
                if not provider.local:
                    # A billed command to a car that may still be asleep is
                    # never sent: any failed wake ends the attempt
                    return _tagged(woke, "wake ")
                # Today's BLE behaviour: the command still goes out
                logger.debug(
                    "%s wake for %s failed (%s); sending anyway",
                    provider.name,
                    vin,
                    woke.kind,
                )
        rateLimits.note(provider, attempt.capability, vin, aspect)
        outcome = checked(
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
            "Stamping %s for %s via %s raised %s",
            attempt.capability,
            vin,
            provider.name,
            type(e).__name__,
        )
    if hooks is not None:
        try:
            hooks.afterWrite(vehicle, attempt, outcome)
        except Exception as e:
            logger.warning(
                "afterWrite for %s %s raised %s",
                attempt.capability,
                vin,
                type(e).__name__,
            )
    return outcome


class Workers:
    def __init__(self, master, report=None, inline=False):
        self.master = master
        self._callback = report
        self.rateLimits = RateLimits()
        self.ble = self._queue("ble", inline)
        self.cloud = self._queue("cloud", inline)

    def _queue(self, name, inline):
        return VehicleCommandQueue(
            name="Vehicle-" + name,
            run=self._run,
            key=entryKey,
            isHigh=isStop,
            report=self._report,
            lost=lambda a, detail: self._report(a, Outcome(ERROR, detail)),
            inline=inline,
        )

    def _run(self, attempt) -> Outcome:
        try:
            return runAttempt(self.master, attempt, self.rateLimits)
        except Exception as e:
            # Never kills the worker: the entry is still reported
            return Outcome(ERROR, type(e).__name__)

    def _report(self, attempt, outcome) -> None:
        if isinstance(outcome, BaseException):
            outcome = Outcome(ERROR, type(outcome).__name__)
        elif not isOutcome(outcome):
            outcome = Outcome(ERROR, "bad return " + type(outcome).__name__)
        if attempt.aspect is None:
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
                "%s %s for %s via %s: %s %s",
                attempt.aspect,
                attempt.capability,
                attempt.key,
                attempt.provider,
                outcome.kind,
                outcome.detail,
            )
        callback = self._callback
        if callback is None:
            return
        try:
            callback(attempt, outcome)
        except Exception as e:
            logger.warning(
                "Vehicle attempt report callback raised %s", type(e).__name__
            )

    def setReport(self, callback) -> None:
        self._callback = callback

    def submit(self, attempt) -> bool:
        # Never under DesiredStore's lock (see the header)
        worker = _workerOf(self.master, attempt.provider)
        if worker == "local":
            # No I/O: only a request the heartbeat sends
            self._report(attempt, self._run(attempt))
            return True
        return (self.ble if worker == "ble" else self.cloud).put(attempt)

    def cancel(self, key) -> int:
        # Never under DesiredStore's lock: losers report on this thread
        # Pending stops for key only; a running attempt is left alone
        return sum(
            q.remove(lambda a: a.key == key and isStop(a))
            for q in (self.ble, self.cloud)
        )

    def allows(self, provider, capability, vin, aspect=None) -> bool:
        return self.rateLimits.allows(provider, capability, vin, aspect)

    def start(self) -> None:
        self.ble.start()
        self.cloud.start()

    def drain(self, timeout) -> bool:
        # Both closed first, then one deadline for both: SIGTERM is never
        # held past timeout
        deadline = time.monotonic() + max(0.0, timeout)
        self.cloud.close()
        self.ble.close()
        cloud = self.cloud.waitDrained(deadline)
        return self.ble.waitDrained(deadline) and cloud

    def pending(self) -> int:
        return self.ble.pending() + self.cloud.pending()

    def summary(self) -> dict:
        return {"ble": self.ble.summary(), "cloud": self.cloud.summary()}
