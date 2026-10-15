# The charge-limit decision rules, from presence/2's ChargeLimitRules.
# decideChargeLimit is pure: no I/O, no master. The reconciler's beforeWrite
# runs it on the worker after the read, then saves, sends or settles as it
# says. The saved record is a HeldLimit (HeldLimits.py): None is nothing
# saved, held None is "we hold none".
from typing import NamedTuple, Optional

from .Desired import CHARGE_LIMIT, CHARGING
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
    # Tesla accepts 50-100 only (ledger L28): a migrated or configured value
    # outside that range is sent at the nearest end
    return min(MAX_LIMIT, max(MIN_LIMIT, limit))


def decideChargeLimit(
    desired, restore: bool, current, held: Optional[HeldLimit], sendWhenUnknown=False
) -> ChargeLimitDecision:
    """Decide how to apply `desired` (50-100), or with restore=True how to
    put back the normal limit.

    current: the car's limit now, or None if it could not be read.
    held: the HeldLimit saved for the car, or None.
    sendWhenUnknown: what an unknown current limit means. False (a cloud
        provider): don't risk changing a limit we cannot see; nothing is
        decided. True (a free provider): send anyway, keeping any saved
        normal limit.

    The rules, with the current limit known:
    - We hold no limit (nothing saved, or held None): the car's limit is
      its normal limit.
    - We hold one, but the car is not at it: it was set by hand, and that is
      the new normal limit.
    - A restore sends the normal limit only while ours is still set;
      otherwise the car's limit stands and becomes the normal limit.
    - A car already at the target gets no command.
    """
    outside = _clamped(held.outside) if held is not None else None
    ours = held.held if held is not None else None
    target = None if restore else _clamped(desired)

    if current is None and not sendWhenUnknown:
        return ChargeLimitDecision(
            None, None, None, False, "could not read the current limit; nothing sent"
        )

    if restore and held is None:
        return ChargeLimitDecision(
            None, None, None, True, "no saved normal limit; nothing to restore"
        )

    if current is None:
        if restore:
            return ChargeLimitDecision(
                outside,
                None,
                HeldLimit(outside, None),
                False,
                "restore to %s%% (current limit unknown)" % outside,
            )
        return ChargeLimitDecision(
            target,
            None,
            HeldLimit(target if held is None else outside, target),
            False,
            "set %s%% (current limit unknown)" % target,
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


def needsLiveRead(restore: bool, telemetryCurrent, held: Optional[HeldLimit]) -> bool:
    """True when a telemetry current limit can't be trusted for this
    decision and the worker must read the car live.

    Only a restore that would hinge on it: while we hold a limit, telemetry
    equal to the held limit confirms ours is still set (the restore is
    sent), but any other value may be a hand-set limit or a stale one, and
    telemetry can't tell those apart. Applies keep the telemetry value."""
    return (
        restore
        and telemetryCurrent is not None
        and held is not None
        and held.held is not None
        and telemetryCurrent != held.held
    )
