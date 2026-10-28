# Run: one vehicle attempt (read before write, wake first, the command, the
# own-write stamp). runAttempt is the one place a vehicle command is issued.
# A local provider (Gen2TWC, Gen3TWC) only publishes a request that the
# serial thread's heartbeat sends, so its attempts run inline on the
# submitter's thread. Attempts carry the registry's time.time stamps, and no
# lock is held while one runs.
import collections
import itertools
import logging
import threading

from ..Presence import NOT_HOME, PRESUMED, REACH_WINDOW, asleep, drivingNow, presence
from ..Providers import Capability as C
from .Desired import AMPS, CHARGE_LIMIT, CHARGING, FIELD_FOR
from . import Outcome as O
from .Belief import uncompleted
from .Rules import VEHICLE_SOURCES_EXCLUDED, satisfied

logger = logging.getLogger(__name__.split(".")[0] + ".Vehicle.Reconcile")

# Commands that take the attempt's value after the vin
_VALUE_COMMANDS = frozenset({C.SET_CHARGE_LIMIT, C.SET_AMPS})
# A provider's SATISFIED is stamped for these only: a SATISFIED start/stop
# leaves the state the read gave
_STAMP_SATISFIED = frozenset({CHARGE_LIMIT, AMPS})
# The wake outcomes after which a non-local provider's command goes out
_WAKE_PROCEEDS = frozenset({O.DONE, O.SATISFIED, O.UNSUPPORTED})


# How long after a charge ending or a plug event the car's next snapshot is
# still worth waiting for
EVENT_WINDOW = 60.0
_EVENT_MARKS = ("drawStop", "plugIn", "unplug")


# One-shot entry keys: a fresh number per one-shot, so none replaces another
_sequence = itertools.count(1)

# (provider, method) already warned about a bad return; bounded by the
# registered providers and their methods
_badReturns = set()
_badLock = threading.Lock()


# Work that is no command (a scan, an attribution, a status or online
# check): one pending per (key, kind), in turn on its worker. done, if
# given, has the result or why it never ran, once
Job = collections.namedtuple("Job", "key kind run done", defaults=(None,))


def entryKey(attempt) -> tuple:
    if isinstance(attempt, Job):
        return (attempt.key, attempt.kind)
    if attempt.aspect is not None:
        return (attempt.key, attempt.aspect)
    return (attempt.key, "debug", next(_sequence))


def isStop(attempt) -> bool:
    return getattr(attempt, "capability", None) == C.STOP_CHARGING


def writtenValue(aspect, value):
    # What the own-write stamp records, in the field's own vocabulary
    if aspect == CHARGING:
        return "Charging" if value is True else "Stopped"
    return value


def checked(provider, method, result) -> O.Outcome:
    # The provider return contract
    if O.isOutcome(result):
        return result
    kind = type(result).__name__
    with _badLock:
        first = (provider.name, method) not in _badReturns
        _badReturns.add((provider.name, method))
    if first:
        logger.warning(
            "%s %s: bad return %s, not an Outcome", provider.name, method, kind
        )
    return O.Outcome(O.ERROR, "bad return " + kind)


def _tagged(outcome, prefix) -> O.Outcome:
    # An outcome that sent no request keeps its NOT_SENT detail
    if O.isNotSent(outcome) or outcome.detail.startswith(prefix.strip()):
        return outcome
    return outcome._replace(detail=(prefix + outcome.detail).rstrip())


def workerOf(provider) -> str:
    # An unregistered provider (None) counts as cloud
    return getattr(provider, "worker", "cloud")


def _call(provider, capability, vin, value):
    # A capability's name is the Provider method's
    method = getattr(provider, capability)
    if capability in _VALUE_COMMANDS:
        return method(vin, value)
    return method(vin)


def _wake(provider, vehicle, capability):
    # The outcome that ends the attempt, or None to send the command
    vin = vehicle.key
    woke = checked(provider, C.WAKE, provider.wake(vin))
    if woke.kind == O.DONE:
        vehicle.observe("state", "online", provider.name)
    logger.info(
        "%s wake for %s before %s: %s",
        provider.name,
        vin,
        capability,
        (woke.kind + " " + woke.detail).strip(),
    )
    if woke.kind == O.REFUSED:
        return _tagged(woke, "wake ")
    if woke.kind not in _WAKE_PROCEEDS:
        if not provider.local:
            # A billed command to a car that may still be asleep is never
            # sent: any failed wake ends the attempt
            return _tagged(woke, "wake ")
        # A local channel (BLE) still sends the command
        logger.debug(
            "%s wake for %s failed (%s); sending anyway",
            provider.name,
            vin,
            woke.kind,
        )
    return None


def runAttempt(reconciler, attempt) -> O.Outcome:
    master = reconciler.master
    vin = attempt.key
    aspect = attempt.aspect
    if aspect is not None and not isStop(attempt):
        # The desire this attempt was made for has changed or gone while it
        # waited: a loser, with no read and no request
        want = reconciler.desired.get(vin, aspect)
        if want is None or want.value != attempt.value:
            return O.Outcome(O.ERROR, "cancelled")
    vehicle = master.vehicleRegistry.get(vin)
    if vehicle is None:
        return O.Outcome(O.ERROR, "no vehicle")
    provider = master.providerOrder.provider(attempt.provider)
    if provider is None:
        return O.Outcome(O.ERROR, "no provider")
    # A cloud provider wakes before its read: an asleep car's value stays
    # unknown, and a billed write never goes blind
    wakeFirst = attempt.wake and not provider.local and provider.declares(C.WAKE)
    try:
        ble = provider.name == "TeslaBLE" and aspect and not isStop(attempt)
        state = presence(master, vehicle)
        if provider.module is not None and (state == NOT_HOME or drivingNow(vehicle)):
            early = reconciler.inapplicable(vehicle, attempt)
            if early is not None:
                return early
        # A message that may not wake leaves a sleeping car alone. The
        # sleep-safe ping first for a car that may be away, or asleep and not
        # seen awake lately
        quiet = ble and not attempt.wake and state != NOT_HOME
        known = asleep(vehicle, master.vehicleRegistry.now() - REACH_WINDOW)
        check = state == PRESUMED or quiet and known is None
        if ble and check and not (quiet and known):
            if not provider.module.ping(vin):
                return O.Outcome(O.UNREACHABLE, "no BLE answer")
        if quiet and asleep(vehicle):
            return O.ASLEEP
        if wakeFirst:
            early = _wake(provider, vehicle, attempt.capability)
            if early is not None:
                return early
        field = FIELD_FOR[aspect] if aspect is not None else None
        read = field is not None and attempt.maxAge is not None
        if read:
            # Only the providers on this provider's own worker
            worker = workerOf(provider)
            # Only the charging state follows these events; a limit or
            # current they leave alone needs no post-event read
            now = master.vehicleRegistry.now()
            event = max(vehicle.markAt(m) for m in _EVENT_MARKS)
            recent = aspect == CHARGING and now - event <= EVENT_WINDOW
            # The snapshot carries no position, and a car whose position is
            # unknown needs one
            skip = ("FleetAPICached",) if state == PRESUMED else ()
            vehicle.read(
                field,
                attempt.maxAge,
                providers=[
                    p
                    for p in master.providerOrder.providers()
                    if workerOf(p) == worker and p.name not in skip
                ],
                force=attempt.force,
                since=event if recent else None,
            )
        believed = (
            vehicle.believed(field, exclude=VEHICLE_SOURCES_EXCLUDED) if field else None
        )
        if aspect == CHARGING:
            believed = uncompleted(vehicle, believed)
        if read and satisfied(aspect, attempt.value, believed):
            return O.Outcome(O.SATISFIED, "read")
        if not (provider.local or attempt.wake or isStop(attempt)):
            # Asleep by its free online check, or known asleep without one:
            # the command cannot reach it
            awake = provider.online(vehicle)
            if awake is False or awake is None and asleep(vehicle):
                return O.ASLEEP
        value, early = reconciler.beforeWrite(vehicle, attempt, provider, believed)
        if early is not None:
            return _tagged(early, "rules: ")
        if attempt.wake and not wakeFirst and provider.declares(C.WAKE):
            early = _wake(provider, vehicle, attempt.capability)
            if early is not None:
                return early
        elif (
            attempt.wakeSkipped
            and provider.declares(C.WAKE)
            and not (isStop(attempt) or attempt.restore)
        ):
            logger.info(
                "%s no wake for %s before %s: %s",
                provider.name,
                vin,
                attempt.capability,
                attempt.wakeSkipped,
            )
        outcome = checked(
            provider,
            attempt.capability,
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
        return O.Outcome(O.ERROR, type(e).__name__)
    # The command has run: whatever fails from here on, its outcome stands. A
    # paid command that took effect must never come back as a retryable ERROR
    try:
        if aspect is not None and (
            outcome.kind == O.DONE
            or (outcome.kind == O.SATISFIED and aspect in _STAMP_SATISFIED)
        ):
            cost = provider.cost(attempt.capability)
            if cost is None or cost.tier != C.TIER_DISRUPTIVE:
                vehicle.noteWrite(field, writtenValue(aspect, value), provider.name)
    except Exception as e:
        logger.warning(
            "Stamping %s for %s via %s raised %s",
            attempt.capability,
            vin,
            provider.name,
            type(e).__name__,
        )
    try:
        reconciler.afterWrite(vehicle, attempt, outcome)
    except Exception as e:
        logger.warning(
            "afterWrite for %s %s raised %s", attempt.capability, vin, type(e).__name__
        )
    return outcome
