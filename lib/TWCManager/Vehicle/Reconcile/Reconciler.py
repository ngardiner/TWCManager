# The vehicle reconciler: one thread that converges each car on its desired
# state through the provider order, one attempt per aspect at a time.
#
# The one fallback rule: free providers first. A paid (billed or disruptive)
# provider is used only when the car has no ready free candidate, or when the
# car's free channel has failed for the aspect's PAID_AFTER window and was
# tried recently (a channel nobody has tried for PROBE_AFTER gets the real
# command over it first). Each desire gets one paid request, plus one blind
# retry after a failure that actually sent and never with a wake, or, after a
# wake the car did not answer, one follow-up through the same provider
# (WAKE_CHECK); after that only a reading that reached the car (or a failed
# wake) or a new desired value sends again, and such a reading re-arms a
# desire once at most. No aspect is sent to a car twice inside MIN_SEND_GAP,
# whichever providers carried it.
#
# It owns the ble and cloud attempt queues. Each accepted attempt is reported
# once: on its queue's thread when it runs, else on the caller's (a local
# provider's always). So no lock is held across a submit or cancel, and
# planning and submitting are kept apart.
#
# One clock domain: every stamp is the registry's time.time.
import logging
import threading
import time
from typing import Any, Dict, Optional, Tuple

from ...VehicleCommandQueue import VehicleCommandQueue
from .. import Presence
from ..Registry.Fields import READ_MAX_AGE
from ..Providers.Capability import (
    NO_INDIVIDUAL_STOP,
    SET_CHARGE_LIMIT,
    START_CHARGING,
    STOP_CHARGING,
    TIER_BILLED,
    WAKE,
)
from . import Run
from .Desired import AMPS, CAPABILITY_FOR, CHARGE_LIMIT, CHARGING, FIELD_FOR, Attempt
from .Outcome import (
    ERROR,
    EXCLUDING,
    HELD,
    QUEUE_DETAILS,
    RULES,
    SATISFIED,
    SETTLED,
    UNREACHABLE,
    Outcome,
    isNotSent,
)
from .Belief import (
    _newer,
    allowWake,
    handsOff,
    known,
    newerReading,
    reachedCar,
)
from .Rules import (
    VEHICLE_SOURCES_EXCLUDED,
    decideChargeLimit,
    needsLiveRead,
    releases,
    satisfied,
)

logger = logging.getLogger(__name__.split(".")[0] + ".Vehicle.Reconcile")

# The longest a pass waits for a wake
TICK = 1.0
# Two knobs that share their values (the 300 s limit gap
# and the 60 s start/stop gate): PAID_AFTER, how long the free channel must
# have failed before a paid provider is used; RETRY, the gap before the same
# aspect is tried again. Split the table to tune one alone
PAID_AFTER = RETRY = {CHARGE_LIMIT: 300.0, CHARGING: 60.0, AMPS: 60.0}
# After a cloud wake the car did not answer, the gap between its follow-ups:
# the provider holds its next wake (Provider.wake), so each is its free
# online check (TeslaAPI caches it 120 s) until the car is online
WAKE_CHECK = 120.0
# A restore to a car that is not answering is retried hourly
RESTORE_RETRY = 3600.0
# A free channel failing but untried for this long gets the real command
# before money is spent
PROBE_AFTER = 600.0
# A start waits this long after a limit write
LIMIT_THEN_START_GAP = 5.0
# The least time between two sends of one aspect to one car, whichever
# providers carry them: it spaces start/stop toggles and rate changes
MIN_SEND_GAP = {CHARGE_LIMIT: 10.0, CHARGING: 10.0, AMPS: 15.0}
# The most paid sends, and wakes and reads for held attempts, per desire
PAID_BUDGET = 4
# A limit held unsent for one of these (both NOT_SENT) cannot be delivered
_REFUSALS = frozenset({"wake refused", "no command token"})

# A car reporting one of these is drawing power, so awake
_DRAWING = frozenset({"Charging", "Starting"})
# The plan order: a limit, then the rate before a start (less overshoot);
# a stop never waits behind a rate
_ORDER, _STOP_ORDER = (CHARGE_LIMIT, AMPS, CHARGING), (CHARGE_LIMIT, CHARGING, AMPS)


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
        self._event = threading.Event()
        self._stopping = False
        # (key, aspect) -> the last hands-off or no-candidate message, so
        # each is logged once; pruned to the store's keys every pass
        self._said: Dict[Tuple[str, str], str] = {}
        self.ble, self.cloud = (
            VehicleCommandQueue(
                name="Vehicle-" + name,
                run=self._run,
                key=Run.entryKey,
                isHigh=Run.isStop,
                # Late-bound, so a replaced report is the one called
                report=lambda a, outcome: self.report(a, outcome),
                lost=lambda a, detail: self.report(a, Outcome(ERROR, detail)),
            )
            for name in ("ble", "cloud")
        )
        self.desired.changed = self.wake

    def wake(self, *args) -> None:
        self._event.set()

    def start(self) -> None:
        self.ble.start()
        self.cloud.start()
        super().start()

    def stop(self) -> None:
        self._stopping = True
        self._event.set()

    def drain(self, timeout) -> bool:
        # Both closed first, then one deadline for both: SIGTERM is never
        # held past timeout. Only pending stops still run
        deadline = time.monotonic() + max(0.0, timeout)
        for q in (self.cloud, self.ble):
            q.close()
        return all([q.waitDrained(deadline) for q in (self.cloud, self.ble)])

    def submit(self, attempt) -> bool:
        worker = Run.workerOf(self.master.providerOrder.provider(attempt.provider))
        if worker == "local":
            # No I/O: only a request the heartbeat sends
            self.report(attempt, self._run(attempt))
            return True
        return (self.ble if worker == "ble" else self.cloud).put(attempt)

    def cancel(self, key) -> int:
        # Pending stops for key only; a running attempt is left alone
        return sum(
            q.remove(lambda a: a.key == key and Run.isStop(a))
            for q in (self.ble, self.cloud)
        )

    def _run(self, attempt) -> Outcome:
        try:
            return Run.runAttempt(self, attempt)
        except Exception as e:
            # Never kills a queue's thread: the entry is still reported
            return Outcome(ERROR, type(e).__name__)

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
            stop = getattr(self.desired.get(key, CHARGING), "value", None) is False
            for aspect in _STOP_ORDER if stop else _ORDER:
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
                accepted = self.submit(attempt)
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
                # Never left in flight (a no-op when the queue reported it)
                self.desired.noteOutcome(
                    attempt.key,
                    attempt.aspect,
                    attempt,
                    Outcome(ERROR, "dropped"),
                    now,
                    paid=False,
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
        if rec.heldAt is not None and vehicle.lastObservedAt <= rec.heldAt:
            # Held: only newer evidence (or a new desire) plans it again
            return None
        k = known(vehicle, aspect)
        if d.lift and (rec.settledAt is not None or satisfied(AMPS, d.value, k)):
            # A lift is a one-shot desire: done once it settles
            self.desired.clearIf(key, AMPS, d.value)
            return None
        if now - rec.lastSentAt < MIN_SEND_GAP[aspect]:
            return None
        last = rec.lastOutcome
        if (
            last is not None
            and rec.heldAt is None
            and isNotSent(last)
            and last.detail not in QUEUE_DETAILS
            and last.kind not in EXCLUDING
            and now - rec.lastOutcomeAt
            < (WAKE_CHECK if rec.wakeCheckBy else RETRY[aspect])
        ):
            # Wake held, backoff, wake refused: no resubmit loop. A
            # refusal excluded its provider, so the next one goes at once
            return None
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
            if d.restore and held is not None and held.held is not None:
                # Already back at the outside limit: nothing is held
                self.master.heldLimits.remove(vehicle.vin)
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
            # Settled without a stamp: wait for a newer reading
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
        # At most two paid sends per arm, and two arms per desire: one send,
        # then one more after a failure or a reading that reached the car
        # A start goes with the limit first: its wake carries the limit
        ride = aspect == CHARGE_LIMIT and self._startWanted(vehicle, config)
        field = FIELD_FOR[aspect]
        # A release goes to the outside limit: as any apply, a raise may wake
        aim = held.outside if releases(d) and held is not None else d.value
        wakes = allowWake(aspect, d._replace(value=aim), k) or ride and not d.restore
        # Nor for a raise its charging point can do nothing with
        idle = aspect != CHARGING and getattr(self.master, "offered", {}).get(key) == 0
        wakes = wakes and not idle
        reached = rec.paidSends and reachedCar(vehicle, field, rec.paidAt, rec.paidSaw)
        if (
            rec.paidSends
            and not rec.paidReached
            and not rec.rearmed
            and now - rec.paidAt >= RETRY[aspect]
            and (
                reached
                or ride
                or self._chargingSince(vehicle, aspect, rec.paidAt)
                or rec.paidSends >= 2
                and rec.wakeCheckBy
                and wakes
            )
        ):
            # No paid send reached the car: a reading shows it awake, a start
            # will wake it, or the arm ended on a failed wake. At most two
            # more sends, once per desire, each with the normal wake and read.
            # Written without the store's lock: a racing newDesire() can
            # inherit rearmed and lose a retry, never gain a paid send
            rec.paidSends = 0
            rec.rearmed = True
            rec.paidRetryDue = False
        paidAgain = rec.paidSends == 0 or (
            rec.paidSends < 2
            and (
                now - rec.paidAt >= RETRY[aspect]
                and (rec.paidRetryDue or reached)
                # Its wake failed: the follow-up through it (WAKE_CHECK)
                or now - rec.paidAt >= WAKE_CHECK
                and wakes
                and paid
                and paid[0].name == rec.wakeCheckBy
            )
        )
        freeGap = RESTORE_RETRY if d.restore and rec.lastFreeAt else RETRY[aspect]
        # Spent only while the message would still not go
        spent = rec.paidSends + rec.paidSpent >= PAID_BUDGET and (
            Presence.applicable(self.master, vehicle, capability)[0] != Presence.SEND
        )
        if paid and freeFailed and paidAgain and not spent:
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
                # Tried, unsettled and not spent
                or dl is not None
                and limit.lastOutcome is not None
                and not satisfied(CHARGE_LIMIT, dl.value, known(vehicle, CHARGE_LIMIT))
                and not self._limitSpent(vehicle, limit)
            ):
                # A limit always goes first, and settles before a start
                return None
        # The blind retry never wakes and reads nothing: the first
        # attempt read. A paid attempt reads only a value older than its
        # whole window
        blind = p in paid and rec.paidRetryDue
        wake = wakes and (p.local or aspect != AMPS) and not blind
        attempt = Attempt(
            key,
            aspect,
            capability,
            d.value,
            p.name,
            wake=wake,
            maxAge=(
                None
                if aspect == AMPS or blind
                else READ_MAX_AGE + (PAID_AFTER[aspect] if p in paid else 0)
            ),
            restore=d.restore,
            # A desire set after this pass began is still this attempt's
            since=max(now, d.at),
            force=aspect == CHARGE_LIMIT
            and needsLiveRead(releases(d), k.value if k else None, held),
            wakeSkipped=None if wake else self._whyNoWake(aspect, p, k, blind, idle),
            paidBefore=vehicle.paidRequests,
        )
        rec.inFlight = attempt
        return attempt

    @staticmethod
    def _whyNoWake(aspect, p, k, blind, idle) -> str:
        if blind:
            return "blind paid retry"
        if idle:
            return "nothing offered"
        if aspect == AMPS and not p.local:
            return "billed provider never wakes for amps"
        if aspect == CHARGE_LIMIT and k is None:
            return "current limit unknown"
        return "lower never wakes"

    def _startWanted(self, vehicle, config) -> bool:
        start = self.desired.get(vehicle.key, CHARGING)
        k = known(vehicle, CHARGING)
        return (
            start is not None
            and start.value is True
            and not satisfied(CHARGING, True, k)
            and handsOff(vehicle, CHARGING, True, k, config) is None
            # A start that would itself spend a cloud wake (not mode 2's)
            and any(
                not p.local and p.declares(WAKE)
                for p in self.master.providerOrder.candidates(vehicle, START_CHARGING)
            )
        )

    @staticmethod
    def _chargingSince(vehicle, aspect, since) -> bool:
        # A car drawing power is awake: a lowered limit gets one more send.
        # Not "Complete": a finished car may be asleep
        return aspect == CHARGE_LIMIT and any(
            obs.value in _DRAWING for obs in _newer(vehicle, FIELD_FOR[CHARGING], since)
        )

    def _limitSpent(self, vehicle, limit) -> bool:
        # The limit cannot be delivered, so it no longer holds the start:
        # every provider that can serve this car is excluded (a paired
        # BLE with its radio down still can: readiness is the claim);
        # both paid sends went to a car that answered; its one re-arm is
        # used with no blind retry left; a refusal holds it unsent; or the
        # car's limit could not be read twice, RETRY apart
        able = self.master.providerOrder.candidates(vehicle, SET_CHARGE_LIMIT)
        last = limit.lastOutcome
        return (
            all(p.name in limit.excluded for p in able)
            or limit.paidSends >= 2
            and limit.paidReached
            or limit.paidSends
            and limit.rearmed
            and not limit.paidRetryDue
            or last is not None
            and last.detail in _REFUSALS
            or limit.unread >= 2
        )

    def _held(self, vehicle):
        return self.master.heldLimits.get(vehicle.vin) if vehicle.vin else None

    def _save(self, vehicle, outside, held) -> None:
        # HeldLimits.set rounds floats (80.0 from Home Assistant)
        if vehicle.vin:
            self.master.heldLimits.set(vehicle.vin, outside=outside, held=held)

    # Hooks: runAttempt calls these on the queue's thread, around the command

    def inapplicable(self, vehicle, attempt) -> Optional[Outcome]:
        # The one applicability rule, after the attempt's read: None sends
        if attempt.aspect is None or Run.isStop(attempt) or attempt.restore:
            return None
        verdict, why = Presence.applicable(self.master, vehicle, attempt.capability)
        if verdict == Presence.SEND:
            return None
        rec = self.desired.record(attempt.key, attempt.aspect)
        if verdict != Presence.HOLD or rec.heldAt is None:
            logger.info("%s %s: %s (%s)", vehicle.key, attempt.aspect, verdict, why)
        if verdict == Presence.HOLD:
            return Outcome(SATISFIED, HELD + why)
        self.desired.clearIf(attempt.key, attempt.aspect, attempt.value)
        return Outcome(SATISFIED, RULES + verdict)

    def beforeWrite(self, vehicle, attempt, provider, believed):
        early = self.inapplicable(vehicle, attempt)
        if early is not None or attempt.aspect != CHARGE_LIMIT:
            return attempt.value, early
        decision = decideChargeLimit(
            attempt.value,
            attempt.restore,
            believed.value if believed is not None else None,
            self._held(vehicle),
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
        # On a queue's or the submitter's thread. Never submits or cancels
        if isinstance(outcome, BaseException):
            # A queue's run raised past _run (SystemExit)
            outcome = Outcome(ERROR, type(outcome).__name__)
        logger.info(
            "%s %s for %s via %s: %s %s",
            attempt.aspect or "Debug",
            attempt.capability,
            attempt.key,
            attempt.provider,
            outcome.kind,
            outcome.detail,
        )
        if attempt.aspect is None:
            # A debug one-shot changes no record
            return
        provider = self.master.providerOrder.provider(attempt.provider)
        vehicle = self.master.vehicleRegistry.get(attempt.key)
        self.desired.noteOutcome(
            attempt.key,
            attempt.aspect,
            attempt,
            outcome,
            self._clock(),
            paid=_tier(provider, attempt.capability) >= TIER_BILLED,
            spent=vehicle.paidRequests - attempt.paidBefore if vehicle else 0,
        )
        self.wake()
