# Run: one vehicle attempt (read before write, wake first, the command, the
# own-write stamp). The reconciler's ble and cloud queues run attempts on
# their own threads; a local provider (Gen2TWC, Gen3TWC) only publishes a
# request that the serial thread's heartbeat sends, so its attempts run
# inline on the submitter's thread. runAttempt is the one place a vehicle
# command is issued, and it calls the reconciler's beforeWrite and
# afterWrite around it.
#
# One clock domain: attempts carry time.time stamps (the registry's). No
# lock is held while running an attempt.
import itertools
import logging
import threading

from ..Providers.Capability import (
    SET_AMPS,
    SET_CHARGE_LIMIT,
    STOP_CHARGING,
    TIER_DISRUPTIVE,
    WAKE,
)
from .Desired import AMPS, CHARGE_LIMIT, CHARGING, FIELD_FOR
from .Outcome import (
    DONE,
    ERROR,
    REFUSED,
    SATISFIED,
    UNSUPPORTED,
    Outcome,
    isNotSent,
    isOutcome,
)
from .Belief import uncompleted
from .Rules import VEHICLE_SOURCES_EXCLUDED, satisfied

logger = logging.getLogger(__name__.split(".")[0] + ".Vehicle.Reconcile")

# Commands that take the attempt's value after the vin
_VALUE_COMMANDS = frozenset({SET_CHARGE_LIMIT, SET_AMPS})
# A provider's SATISFIED is stamped for these only: a SATISFIED start/stop
# leaves the state the read gave
_STAMP_SATISFIED = frozenset({CHARGE_LIMIT, AMPS})
# The wake outcomes after which a non-local provider's command goes out
_WAKE_PROCEEDS = frozenset({DONE, SATISFIED, UNSUPPORTED})


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
    # An outcome that sent no request keeps its NOT_SENT detail
    if isNotSent(outcome) or outcome.detail.startswith(prefix.strip()):
        return outcome
    return outcome._replace(detail=(prefix + outcome.detail).rstrip())


def workerOf(master, providerName) -> str:
    # An unregistered provider counts as cloud
    provider = master.providerOrder.provider(providerName)
    return "cloud" if provider is None else getattr(provider, "worker", "cloud")


def _call(provider, capability, vin, value):
    # A capability's name is the Provider method's
    method = getattr(provider, capability)
    if capability in _VALUE_COMMANDS:
        return method(vin, value)
    return method(vin)


def _wake(provider, vin, capability):
    # The outcome that ends the attempt, or None to send the command
    woke = checked(provider, WAKE, provider.wake(vin))
    logger.info(
        "%s wake for %s before %s: %s",
        provider.name,
        vin,
        capability,
        (woke.kind + " " + woke.detail).strip(),
    )
    if woke.kind == REFUSED:
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


def runAttempt(reconciler, attempt) -> Outcome:
    master = reconciler.master
    vin = attempt.key
    aspect = attempt.aspect
    if aspect is not None and not isStop(attempt):
        # The desire this attempt was made for has changed or gone while it
        # waited: a loser, with no read and no request
        want = reconciler.desired.get(vin, aspect)
        if want is None or want.value != attempt.value:
            return Outcome(ERROR, "cancelled")
    vehicle = master.vehicleRegistry.get(vin)
    if vehicle is None:
        return Outcome(ERROR, "no vehicle")
    provider = master.providerOrder.provider(attempt.provider)
    if provider is None:
        return Outcome(ERROR, "no provider")
    # A cloud provider wakes before its read: an asleep car's value stays
    # unknown, and a billed write never goes blind
    wakeFirst = attempt.wake and not provider.local and provider.declares(WAKE)
    try:
        if wakeFirst:
            early = _wake(provider, vin, attempt.capability)
            if early is not None:
                return early
        field = FIELD_FOR[aspect] if aspect is not None else None
        read = field is not None and attempt.maxAge is not None
        if read:
            # Only the providers on this provider's own worker
            worker = getattr(provider, "worker", "cloud")
            vehicle.read(
                field,
                attempt.maxAge,
                providers=[
                    p
                    for p in master.providerOrder.providers()
                    if getattr(p, "worker", "cloud") == worker
                ],
                force=attempt.force,
            )
        believed = (
            vehicle.believed(field, exclude=VEHICLE_SOURCES_EXCLUDED) if field else None
        )
        if aspect == CHARGING:
            believed = uncompleted(vehicle, believed)
        if read and satisfied(aspect, attempt.value, believed):
            return Outcome(SATISFIED, "read")
        value, early = reconciler.beforeWrite(vehicle, attempt, provider, believed)
        if early is not None:
            return _tagged(early, "rules: ")
        if attempt.wake and not wakeFirst and provider.declares(WAKE):
            early = _wake(provider, vin, attempt.capability)
            if early is not None:
                return early
        elif (
            attempt.wakeSkipped
            and provider.declares(WAKE)
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
    try:
        reconciler.afterWrite(vehicle, attempt, outcome)
    except Exception as e:
        logger.warning(
            "afterWrite for %s %s raised %s", attempt.capability, vin, type(e).__name__
        )
    return outcome
