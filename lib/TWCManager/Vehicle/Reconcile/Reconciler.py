# The vehicle reconciler: one thread that converges each car on its desired
# state through the provider order, one attempt per aspect at a time.
#
# The one fallback rule: free providers first. A paid (billed or disruptive)
# provider is used only when the car has no ready free candidate, or when the
# car's free channel has failed for the aspect's PAID_AFTER window and was
# tried recently (a channel nobody has tried for PROBE_AFTER gets the real
# command over it first). Each desire gets one paid request, plus one blind
# retry after a failure that actually sent and never with a wake; after that
# only a newer vehicle reading or a new desired value sends again.
#
# One clock domain: every stamp is the registry's time.time. No lock is held
# across a submit: planning and submitting are kept apart, because local and
# inline attempts report before submit returns.
import logging
import threading
import time
from typing import Any, Dict, Optional, Tuple

from ..Providers.Capability import NO_INDIVIDUAL_STOP, STOP_CHARGING, TIER_BILLED
from .Desired import AMPS, CAPABILITY_FOR, CHARGE_LIMIT, CHARGING, FIELD_FOR, Attempt
from .Outcome import (
    ERROR,
    LANE_DETAILS,
    SATISFIED,
    SETTLED,
    UNREACHABLE,
    Outcome,
    isNotSent,
)
from .Rules import decideChargeLimit, needsLiveRead

logger = logging.getLogger(__name__.split(".")[0] + ".Vehicle.Reconcile")

# The longest a pass waits for a wake
TICK = 1.0
# How long the free channel must have failed before a paid provider is used,
# and the gap before the same aspect is tried again: CarApiVehicle's 300 s
# limit gap and the 60 s start/stop gate (one table, two names)
PAID_AFTER = RETRY = {CHARGE_LIMIT: 300.0, CHARGING: 60.0, AMPS: 60.0}
# A restore to a car that is not answering is retried hourly
RESTORE_RETRY = 3600.0
# A free channel failing but untried for this long gets the real command
# before money is spent
PROBE_AFTER = 600.0
# The read before a write accepts a value this old (CarApiVehicle.READ_MAX_AGE)
READ_MAX_AGE = 300.0
# A start waits this long after a limit write (today's 5 s sleep)
LIMIT_THEN_START_GAP = 5.0
# Observations that never adopt and never stand for the car's own state
VEHICLE_SOURCES_EXCLUDED = frozenset({"TWC", "Gen2TWC", "Gen3TWC"})
# TWC current at or above this means the car is drawing power
POWER_FLOWING_AMPS = 1.0

# The plan order: a limit is planned before a start
_ORDER = (CHARGE_LIMIT, CHARGING, AMPS)
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


def _standIn(vehicle):
    # The newest TWC current, as the car's charging state
    reads = [vehicle.reportedBy("actualCurrent", s) for s in VEHICLE_SOURCES_EXCLUDED]
    reads = [r for r in reads if r is not None and r.value is not None]
    if not reads:
        return None
    obs = max(reads, key=lambda r: r.at)
    flowing = obs.value >= POWER_FLOWING_AMPS
    return obs._replace(value="Charging" if flowing else "Stopped")


def known(vehicle, aspect):
    obs = vehicle.believed(FIELD_FOR[aspect], exclude=VEHICLE_SOURCES_EXCLUDED)
    if obs is None and aspect == CHARGING:
        # A car no vehicle source describes (anonymous, non-Tesla): the TWC
        # current stands in. It never adopts
        return _standIn(vehicle)
    return obs


def newerReading(vehicle, aspect, since, unlike=None) -> bool:
    # A reading of the value unlike (what a paid send saw), or of None, is
    # no evidence (I2)
    field = FIELD_FOR[aspect]
    obs = vehicle.latestFrom(field, exclude=VEHICLE_SOURCES_EXCLUDED)
    if aspect == CHARGING and vehicle.believed(field, VEHICLE_SOURCES_EXCLUDED) is None:
        # Only while known uses the stand-in: for a car a vehicle source
        # describes, the heartbeat's readings are never evidence
        obs = _standIn(vehicle)
    return obs is not None and obs.at > since and obs.value != unlike


def handsOff(vehicle, aspect, desired, known, config=None) -> Optional[str]:
    if aspect != CHARGING or desired is not True:
        return None
    if known is not None and known.value in ("Disconnected", "NoPower"):
        return "the car cannot charge (%s)" % known.value
    if vehicle.value("scheduledChargingPending") is True and (config or {}).get(
        "respectVehicleSchedule", True
    ):
        # As car_api_charge: a start would override the car's own schedule
        return "scheduled charging is pending (respectVehicleSchedule)"
    return None


def allowWake(aspect, desired, known) -> bool:
    # A raise or a start; never a lower, a stop or a restore. A billed
    # provider never wakes for amps (the caller's rule)
    if desired.restore:
        return False
    if aspect == CHARGE_LIMIT:
        try:
            return known is not None and desired.value > known.value
        except TypeError:
            return False
    return desired.value is True if aspect == CHARGING else aspect == AMPS


def _tier(provider, capability) -> int:
    # An unknown provider or cost counts as paid: it fails closed
    cost = provider.cost(capability) if provider is not None else None
    return TIER_BILLED if cost is None else cost.tier


class VehicleReconciler(threading.Thread):
    def __init__(self, master, clock=time.time):
        super().__init__(daemon=True, name="VehicleReconciler")
        self.master = master
        self._clock = clock
        self.desired = master.desired
        self.workers = master.workers
        self._event = threading.Event()
        self._stopping = False
        # (key, aspect) -> the last hands-off or no-candidate message, so
        # each is logged once; pruned to the store's keys every pass
        self._said: Dict[Tuple[str, str], str] = {}
        self.desired.onChange(self.wake)
        self.workers.setReport(self.report)

    def wake(self, *args) -> None:
        self._event.set()

    def stop(self) -> None:
        self._stopping = True
        self._event.set()

    def run(self) -> None:
        while not self._stopping:
            self._event.wait(TICK)
            self._event.clear()
            if self._stopping:
                break
            try:
                self.pass_()
            except Exception as e:
                logger.warning("Vehicle reconciler pass raised %s", type(e).__name__)

    def _sayOnce(self, key, aspect, message, level) -> None:
        if self._said.get((key, aspect)) != message:
            self._said[(key, aspect)] = message
            logger.log(level, "%s %s: %s", key, aspect, message)

    def pass_(self, now=None) -> int:
        now = self._clock() if now is None else now
        keys = self.desired.keys()
        self._said = {k: m for k, m in self._said.items() if k[0] in keys}
        planned = []
        for key in keys:
            vehicle = self.master.vehicleRegistry.get(key)
            if vehicle is None:
                continue
            for aspect in _ORDER:
                try:
                    attempt = self.plan(vehicle, aspect, now)
                except Exception as e:
                    logger.warning(
                        "Planning %s for %s raised %s", aspect, key, type(e).__name__
                    )
                    continue
                if attempt is not None:
                    planned.append(attempt)
        submitted = 0
        for attempt in planned:
            try:
                accepted = self.workers.submit(attempt)
            except Exception as e:
                logger.warning(
                    "Submitting %s for %s raised %s",
                    attempt.aspect,
                    attempt.key,
                    type(e).__name__,
                )
                accepted = False
            if accepted:
                submitted += 1
            else:
                # Never left in flight (a no-op when the worker reported it)
                self.desired.noteOutcome(
                    attempt.key,
                    attempt.aspect,
                    attempt,
                    Outcome(ERROR, "dropped"),
                    now,
                    paid=False,
                    freeProvider=False,
                )
        return submitted

    def _adoptable(self, vehicle, d) -> Optional[Any]:
        # A vehicle read newer than our write and the desire, disagreeing
        # with what we wrote, is the user's change: the value, else None
        field = FIELD_FOR[CHARGE_LIMIT]
        latest = vehicle.latestFrom(field, exclude=VEHICLE_SOURCES_EXCLUDED)
        write = vehicle.lastWrite(field)
        if latest is None or write is None or latest.at <= max(write.at, d.at):
            return None
        if vehicle.trusted(field, exclude=VEHICLE_SOURCES_EXCLUDED) != latest:
            return None
        return latest.value if latest.value != write.value else None

    def plan(self, vehicle, aspect, now) -> Optional[Attempt]:
        key = vehicle.key
        d = self.desired.get(key, aspect)
        rec = self.desired.record(key, aspect)
        if d is None or rec.inFlight is not None:
            return None
        last = rec.lastOutcome
        if (
            last is not None
            and isNotSent(last)
            and last.detail not in LANE_DETAILS
            and now - rec.lastOutcomeAt < RETRY[aspect]
        ):
            # Wake held, backoff, wake refused: no resubmit loop (H2)
            return None
        k = known(vehicle, aspect)
        if aspect == CHARGE_LIMIT:
            adopted = self._adoptable(vehicle, d)
            if adopted is not None:
                self.desired.adopt(key, aspect, adopted)
                self._save(vehicle, adopted, None)
                logger.info(
                    "%s charge limit set elsewhere to %s; adopted", key, adopted
                )
                return None
        held = self._held(vehicle)
        if satisfied(aspect, d.value, k):
            if d.lift:
                # A lift is a one-shot desire
                self.desired.clearIf(key, AMPS, d.value)
            elif d.restore and held is not None and held.held is not None:
                # Already back at the outside limit: nothing is held (M1)
                self._save(vehicle, k.value, None)
                self.desired.clear(key, aspect)
            return None
        if d.restore and (held is None or held.held is None):
            # Nothing to restore
            self.desired.clear(key, aspect)
            return None
        config = (getattr(self.master, "config", None) or {}).get("config")
        reason = handsOff(vehicle, aspect, d.value, k, config)
        if reason is not None:
            self._sayOnce(key, aspect, "hands off: " + reason, logging.INFO)
            return None
        if rec.settledAt is not None and not (
            newerReading(vehicle, aspect, rec.settledAt)
            and now - rec.lastSentAt >= RETRY[aspect]
        ):
            # Settled without a stamp: wait for a newer reading (N1)
            return None
        capability = CAPABILITY_FOR(aspect, d.value)
        cands = [
            p
            for p in self.master.providerOrder.candidates(
                vehicle,
                capability,
                amps=d.value if aspect == AMPS else None,
                localRule=aspect == AMPS and not d.lift,
            )
            if p.name not in rec.excluded
            # The TWC target is the distributor's
            and not (aspect == AMPS and p.module is None)
            and not (
                capability == STOP_CHARGING
                and NO_INDIVIDUAL_STOP in p.limitations(vehicle)
            )
            and self.workers.allows(p, capability, key, aspect)
        ]
        if not cands:
            self._sayOnce(key, aspect, "no provider can deliver", logging.DEBUG)
            return None
        self._said.pop((key, aspect), None)
        free = [p for p in cands if _tier(p, capability) < TIER_BILLED]
        paid = [p for p in cands if _tier(p, capability) >= TIER_BILLED]
        ch = self.desired.free(key)
        freeFailed = not free or (
            ch.failingSince is not None
            and now - ch.failingSince >= PAID_AFTER[aspect]
            and now - ch.lastAttemptAt < PROBE_AFTER
        )
        # At most two paid sends per desire: one, then one more after a
        # failure or a reading of a new value (I2)
        # A start goes with the limit first: its wake carries the limit
        ride = aspect == CHARGE_LIMIT and self._startWanted(vehicle, config)
        if (
            rec.paidSends
            and not rec.paidReached
            and now - rec.paidAt >= RETRY[aspect]
            and (newerReading(vehicle, aspect, rec.paidAt) or ride)
        ):
            # No paid send reached the car: a reading shows it awake, or a
            # start will wake it. Two more sends (N1)
            rec.paidSends = 0
        paidAgain = rec.paidSends == 0 or (
            rec.paidSends < 2
            and now - rec.paidAt >= RETRY[aspect]
            and (
                rec.paidRetryDue
                or newerReading(vehicle, aspect, rec.paidAt, rec.paidSaw)
            )
        )
        freeGap = RESTORE_RETRY if d.restore and rec.lastFreeAt else RETRY[aspect]
        if paid and freeFailed and paidAgain:
            p = paid[0]
            rec.paidSaw = k.value if k is not None else None
        elif free and now - rec.lastFreeAt >= freeGap:
            p = free[0]
        else:
            return None
        if aspect == CHARGING and d.value is True:
            dl = self.desired.get(key, CHARGE_LIMIT)
            limit = self.desired.record(key, CHARGE_LIMIT)
            write = vehicle.lastWrite(FIELD_FOR[CHARGE_LIMIT])
            if (
                limit.inFlight is not None
                or (write is not None and now - write.at < LIMIT_THEN_START_GAP)
                # Tried, unsettled and not spent on a car that answered
                or dl is not None
                and limit.lastOutcome is not None
                and not satisfied(CHARGE_LIMIT, dl.value, known(vehicle, CHARGE_LIMIT))
                and not (limit.paidSends >= 2 and limit.paidReached)
            ):
                # A limit always goes first, and settles before a start
                return None
        # The blind retry never wakes (M8) and reads nothing: the first
        # attempt read. A paid attempt reads only a value older than its
        # whole window (I3)
        blind = p in paid and rec.paidRetryDue
        attempt = Attempt(
            key,
            aspect,
            capability,
            d.value,
            p.name,
            wake=(allowWake(aspect, d, k) or ride and not d.restore)
            and (p.local or aspect != AMPS)
            and not blind,
            maxAge=(
                None
                if aspect == AMPS or blind
                else READ_MAX_AGE + (PAID_AFTER[aspect] if p in paid else 0)
            ),
            restore=d.restore,
            # A desire set after this pass began is still this attempt's (N2)
            since=max(now, d.at),
            force=aspect == CHARGE_LIMIT
            and needsLiveRead(d.restore, k.value if k else None, held),
        )
        rec.inFlight = attempt
        return attempt

    def _startWanted(self, vehicle, config) -> bool:
        start = self.desired.get(vehicle.key, CHARGING)
        k = known(vehicle, CHARGING)
        return (
            start is not None
            and start.value is True
            and not satisfied(CHARGING, True, k)
            and handsOff(vehicle, CHARGING, True, k, config) is None
        )

    def _held(self, vehicle):
        return self.master.heldLimits.get(vehicle.vin) if vehicle.vin else None

    def _save(self, vehicle, outside, held) -> None:
        # HeldLimits.set rounds floats (80.0 from Home Assistant) (L26)
        if vehicle.vin:
            self.master.heldLimits.set(vehicle.vin, outside=outside, held=held)

    # Hooks: runAttempt calls these on the worker's thread, around the command

    def beforeWrite(self, vehicle, attempt, provider, believed):
        if attempt.aspect != CHARGE_LIMIT:
            return attempt.value, None
        decision = decideChargeLimit(
            attempt.value,
            attempt.restore,
            believed.value if believed is not None else None,
            self._held(vehicle),
            # A free provider always sends, so a free channel that cannot
            # reach the car fails a real request and starts the paid
            # fallback timer; a paid one never sends blind
            sendWhenUnknown=_tier(provider, attempt.capability) < TIER_BILLED,
        )
        if decision.saveNow is not None:
            self._save(vehicle, *decision.saveNow)
        if decision.settled:
            if attempt.restore:
                # Nothing left to restore: the desire is done
                self.desired.clearIf(attempt.key, CHARGE_LIMIT, attempt.value)
            return None, Outcome(SATISFIED, decision.reason)
        if decision.send is None:
            return None, Outcome(UNREACHABLE, decision.reason)
        # Only this worker touches it while the attempt is in flight
        self.desired.record(attempt.key, CHARGE_LIMIT).pendingSave = (
            decision.saveOnSuccess
        )
        return decision.send, None

    def afterWrite(self, vehicle, attempt, outcome) -> None:
        if attempt.aspect == CHARGE_LIMIT:
            rec = self.desired.record(attempt.key, CHARGE_LIMIT)
            save, rec.pendingSave = rec.pendingSave, None
            if save is not None and outcome.kind in SETTLED:
                self._save(vehicle, *save)
        elif (
            attempt.aspect == CHARGING
            and attempt.value is False
            and outcome.kind in SETTLED
        ):
            # A start goes back through the provider that stopped the car
            self.desired.state(attempt.key).lastStopProvider = attempt.provider

    def report(self, attempt, outcome) -> None:
        # On the worker's or the submitter's thread. Never submits or cancels
        if attempt.aspect is None:
            # A debug one-shot: Workers._report has logged it
            return
        tier = _tier(
            self.master.providerOrder.provider(attempt.provider), attempt.capability
        )
        self.desired.noteOutcome(
            attempt.key,
            attempt.aspect,
            attempt,
            outcome,
            self._clock(),
            paid=tier >= TIER_BILLED,
            freeProvider=tier < TIER_BILLED,
        )
        self.wake()

    def summary(self, key) -> dict:
        summary = self.desired.summary(key)
        vehicle = self.master.vehicleRegistry.get(key)
        held = self._held(vehicle) if vehicle is not None else None
        summary["heldLimit"] = held._asdict() if held is not None else None
        return summary
