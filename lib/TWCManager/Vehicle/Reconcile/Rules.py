# The charge-limit decision rules. decideChargeLimit is pure: no I/O, no
# master. The reconciler's beforeWrite runs it on the worker after the read,
# then saves, sends or settles as it says. The saved record is a HeldLimit (HeldLimits.py): None is nothing
# held; a save with held None removes the record.
from typing import NamedTuple, Optional

from .Desired import CHARGE_LIMIT, CHARGING, RELEASE
from .HeldLimits import MAX_LIMIT, MIN_LIMIT, HeldLimit

# Observations that never adopt and never stand for the car's own state
VEHICLE_SOURCES_EXCLUDED = frozenset({"TWC", "Gen2TWC", "Gen3TWC"})
_CHARGING_LIKE = frozenset({"Charging", "Starting", "Complete"})
_STOPPED_LIKE = frozenset({"Stopped", "Complete", "Disconnected", "NoPower"})


def satisfied(aspect, desired, believed) -> bool:
    if believed is None:
        return False
    value = believed.value
    if aspect == CHARGE_LIMIT:
        return value == desired
    if aspect == CHARGING:
        return value in (_CHARGING_LIKE if desired is True else _STOPPED_LIKE)
    try:
        return int(value) == int(desired)
    except (TypeError, ValueError, OverflowError):
        return False


class ChargeLimitDecision(NamedTuple):
    # The limit to send, or None for no command
    send: Optional[int]
    # The HeldLimit to save now, with no send; or None
    saveNow: Optional[HeldLimit]
    # The HeldLimit to save once the send succeeds; or None
    saveOnSuccess: Optional[HeldLimit]
    # True when nothing needs sending and the outcome is final; False when
    # nothing was decided (the current limit is unknown) or a send is pending
    settled: bool
    # A short phrase for the log line
    reason: str


def _clamped(limit):
    # Tesla accepts 50-100 only: a migrated or configured value
    # outside that range is sent at the nearest end
    return min(MAX_LIMIT, max(MIN_LIMIT, limit))


def decideChargeLimit(
    desired, restore: bool, current, held: Optional[HeldLimit]
) -> ChargeLimitDecision:
    """Decide how to apply `desired` (50-100), or with restore=True how to
    put back the normal limit.

    current: the car's limit now, or None if no source knows it: then
        nothing is decided or sent (the reconciler retries).
    held: the HeldLimit saved for the car, or None.

    With the current limit known, a limit we don't hold is the car's normal
    one, and a held limit the car is no longer at was set by hand and is the
    new normal one.
    """
    # A release puts back the outside limit as a restore does
    restore = restore or desired == RELEASE
    outside = _clamped(held.outside) if held is not None else None
    ours = held.held if held is not None else None
    target = None if restore else _clamped(desired)

    if current is None:
        return ChargeLimitDecision(
            None, None, None, False, "could not read the current limit; nothing sent"
        )

    if restore and held is None:
        return ChargeLimitDecision(
            None, None, None, True, "no saved normal limit; nothing to restore"
        )

    handSet = ours is not None and current != ours
    normal = outside if ours is not None and not handSet else current

    if restore:
        if ours is not None and not handSet and current != normal:
            return ChargeLimitDecision(
                normal, None, HeldLimit(normal, None), False, "restore to %s%%" % normal
            )
        if handSet:
            reason = "set by hand to %s%%; nothing to restore" % current
        else:
            reason = "already at %s%%; nothing to restore" % current
        return ChargeLimitDecision(None, HeldLimit(current, None), None, True, reason)

    if current == target:
        return ChargeLimitDecision(
            None,
            HeldLimit(normal, target),
            None,
            True,
            "already at %s%%; nothing sent" % target,
        )
    if handSet:
        reason = "set %s%%; the hand-set %s%% is the new normal" % (target, current)
    else:
        reason = "set %s%%" % target
    return ChargeLimitDecision(target, None, HeldLimit(normal, target), False, reason)


def releases(desired) -> bool:
    # A departure's restore or a policy release: the outside limit goes back
    return desired.restore or desired.value == RELEASE


def needsLiveRead(restore: bool, telemetryCurrent, held: Optional[HeldLimit]) -> bool:
    """True when a restore hinges on a telemetry limit that can't be
    trusted: any value other than the held one may be hand-set or stale, and
    telemetry can't tell which. Applies keep the telemetry value."""
    return (
        restore
        and telemetryCurrent is not None
        and held is not None
        and held.held is not None
        and telemetryCurrent != held.held
    )
