# The vehicle reconciler: one thread that converges each car on its desired
# state through the provider order, one attempt per aspect at a time.
#
# Free providers go first: a paid provider is used only when the car has no
# ready free candidate, or its free channel has failed for the aspect's
# PAID_AFTER window. Each desire gets one paid request, a blind retry after
# a failure that sent, or one WAKE_CHECK follow-up after an unanswered wake;
# after that only a reading that reached the car or a new desired value
# sends again. No aspect goes to a car twice inside MIN_SEND_GAP.
#
# Each accepted attempt is reported once, on its queue's thread when it
# runs, else on the caller's. No lock is held across a submit or cancel, so
# planning and submitting are kept apart. Every stamp is the registry's
# time.time.
import logging
import threading
import time
from typing import Any, Dict, Optional, Tuple

from ...VehicleCommandQueue import VehicleCommandQueue
from .. import Presence, Watch
from ..Registry.Fields import READ_MAX_AGE
from ..Providers import Capability as C
from . import Run
from .Desired import AMPS, CAPABILITY_FOR, CHARGE_LIMIT, CHARGING, FIELD_FOR, Attempt
from . import Outcome as O
from .Belief import known, newerReading, newerReadings, reachedCar
from .Belief import whyHandsOff, whyQuiet
from .Rules import TWC_SOURCES, decideChargeLimit, needsLiveRead, releases, satisfied

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
# The most paid sends per arm: one send, then one more after a failure or
# a reading that reached the car. A desire is re-armed at most once
PAID_SENDS_PER_ARM = 2
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
    return C.TIER_BILLED if cost is None else cost.tier


class VehicleReconciler(threading.Thread):
    # After a restart, nothing may have given a car's charging state yet.
    # Such a car may be woken for a start once per run, and that wake
    # learns the state. False never wakes it for this.
    wakeUnreadOnce = True

    def __init__(self, master, clock=time.time):
        super().__init__(daemon=True, name="VehicleReconciler")
        self.master = master
        self._clock = clock
        self.desired = master.desired
        self._event = threading.Event()
        self._stopping = False
        # (key, aspect) -> the last hands-off or no-candidate message, so
        # each is logged once; pruned to the store's keys every pass
        self._blockedReasons: Dict[Tuple[str, str], str] = {}
        # The cars a start has woken, once its wake was sent
        self._wokeUnread = set()
        self.ble, self.cloud = (
            VehicleCommandQueue(
                name="Vehicle-" + name,
                run=self._run,
                key=Run.entryKey,
                isHigh=Run.isStop,
                # Late-bound, so a replaced report is the one called
                report=lambda a, outcome: self.report(a, outcome),
                lost=lambda a, detail: self.report(a, O.Outcome(O.ERROR, detail)),
            )
            for name in ("ble", "cloud")
        )
        self.desired.changed = self.nudge

    def nudge(self, *args) -> None:
        # Runs a pass now rather than at the next TICK
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

    def submitJob(self, key, kind, run, worker="ble", done=None) -> bool:
        queue = self.cloud if worker == "cloud" else self.ble
        return queue.put(Run.Job(key, kind, run, done))

    def cancel(self, key) -> int:
        # Pending stops for key only; a running attempt is left alone
        return sum(
            q.remove(lambda a: a.key == key and Run.isStop(a))
            for q in (self.ble, self.cloud)
        )

    def _run(self, attempt) -> O.Outcome:
        if isinstance(attempt, Run.Job):
            # The queue logs a job that raises
            return attempt.run()
        try:
            return Run.runAttempt(self, attempt)
        except Exception as e:
            # Never kills a queue's thread: the entry is still reported
            return O.Outcome(O.ERROR, type(e).__name__)

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

    def _logBlockedOnce(self, key, aspect, message, level) -> None:
        if self._blockedReasons.get((key, aspect)) != message:
            self._blockedReasons[(key, aspect)] = message
            logger.log(level, "%s %s: %s", key, aspect, message)

    def blockedReason(self, key, aspect) -> Optional[str]:
        return self._blockedReasons.get((key, aspect))

    def pass_(self, now=None) -> int:
        now = self._clock() if now is None else now
        keys = self.desired.keys()
        self._blockedReasons = {
            k: m for k, m in self._blockedReasons.items() if k[0] in keys
        }
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
                    O.Outcome(O.ERROR, "dropped"),
                    now,
                    paid=False,
                )
        return submitted

    def _adoptable(self, vehicle, want) -> Optional[Any]:
        # A vehicle read newer than our write and the desire, disagreeing
        # with what we wrote, is the user's change: the value, else None
        field = FIELD_FOR[CHARGE_LIMIT]
        latest = vehicle.latestFrom(field, exclude=TWC_SOURCES)
        write = vehicle.lastWrite(field)
        if latest is None or write is None or latest.at <= max(write.at, want.at):
            return None
        if vehicle.trusted(field, exclude=TWC_SOURCES) != latest:
            return None
        return latest.value if latest.value != write.value else None

    def plan(self, vehicle, aspect, now) -> Optional[Attempt]:
        key = vehicle.key
        want = self.desired.get(key, aspect)
        rec = self.desired.record(key, aspect)
        if want is None or rec.inFlight is not None:
            return None
        # A drive's hold can change only once the car stops
        driving = rec.lastOutcome == O.Outcome(O.SATISFIED, O.HELD + "driving")
        if driving:
            _, decidedBy, _ = Presence.decide(self.master, vehicle)
            driving = decidedBy == "driving"
        if self._asleepCheckDue(vehicle, rec, now):
            # The free online check alone: its answer is an observation, so
            # an awake car is a sign of life that plans this again, and an
            # asleep one is no news
            p = self.master.providerOrder.provider(rec.lastProvider)
            rec.checkedAt = now
            self.submitJob(key, "onlineCheck", lambda: p.online(vehicle), "cloud")
            return None
        waiting = rec.heldAt is not None
        if waiting and (vehicle.lastObservedAt <= rec.heldAt or driving):
            # Held: only newer evidence (or a new desire) plans it again. The
            # exception is an asleep hold, which went without a wake because
            # the car was not then expected to charge. Once it is (admitted,
            # its plug no longer known unplugged), that changed here, with no
            # word from the car.
            asleepHoldNowWanted = (
                not (driving or want.restore or want.value is False)
                and rec.lastOutcome == O.ASLEEP
                and not whyQuiet(self.master, vehicle)
            )
            if not asleepHoldNowWanted:
                return None
        belief = known(vehicle, aspect, want.value)
        if want.lift and (
            rec.settledAt is not None or satisfied(AMPS, want.value, belief)
        ):
            # A lift is a one-shot desire: done once it settles
            self.desired.clearIf(key, AMPS, want.value)
            return None
        if now - rec.lastSentAt < MIN_SEND_GAP[aspect]:
            return None
        last = rec.lastOutcome
        if (
            last is not None
            and rec.heldAt is None
            and O.isNotSent(last)
            and last.detail not in O.QUEUE_DETAILS
            and last.kind not in O.EXCLUDING
            and now - rec.lastOutcomeAt
            < (WAKE_CHECK if rec.wakeCheckBy else RETRY[aspect])
        ):
            # Wake held, backoff, wake refused: no resubmit loop. A
            # refusal excluded its provider, so the next one goes at once
            return None
        if aspect == CHARGE_LIMIT:
            adopted = self._adoptable(vehicle, want)
            if adopted is not None:
                self.desired.adopt(key, aspect, adopted)
                self._save(vehicle, adopted, None)
                logger.info(
                    "%s charge limit set elsewhere to %s; adopted", key, adopted
                )
                return None
        saved = self._saved(vehicle)
        if satisfied(aspect, want.value, belief):
            if want.restore:
                # Back at the outside limit: nothing is held, and nothing is
                # left to want; a lingering restore would adopt a limit set
                # away and hold off the policy's limit on the way back
                if saved is not None and saved.held is not None:
                    self.master.heldLimits.remove(vehicle.vin)
                self.desired.clear(key, aspect)
            return None
        if want.restore and (saved is None or saved.held is None):
            # Nothing to restore
            self.desired.clear(key, aspect)
            return None
        config = (getattr(self.master, "config", None) or {}).get("config")
        reason = whyHandsOff(vehicle, aspect, want.value, belief, config)
        if reason is not None:
            self._logBlockedOnce(key, aspect, "hands off: " + reason, logging.INFO)
            return None
        if rec.settledAt is not None and not (
            newerReading(vehicle, aspect, rec.settledAt)
            and now - rec.lastSentAt >= RETRY[aspect]
        ):
            # Settled without a stamp: wait for a newer reading
            return None
        capability = CAPABILITY_FOR(aspect, want.value)
        cands = [
            p
            for p in self.master.providerOrder.candidates(
                vehicle,
                capability,
                amps=want.value if aspect == AMPS else None,
                localRule=aspect == AMPS and not want.lift,
            )
            if p.name not in rec.excluded
            # The TWC target is the distributor's
            and not (aspect == AMPS and p.module is None)
            and not (
                capability == C.STOP_CHARGING
                and C.NO_INDIVIDUAL_STOP in p.limitations(vehicle)
            )
        ]
        if not cands:
            self._logBlockedOnce(key, aspect, "no provider can deliver", logging.DEBUG)
            return None
        self._blockedReasons.pop((key, aspect), None)
        free = [p for p in cands if _tier(p, capability) < C.TIER_BILLED]
        paid = [p for p in cands if _tier(p, capability) >= C.TIER_BILLED]
        freeChannel = self.desired.free(key)
        # Free failures from before the car came back say nothing of it here
        returnedAt = Presence.returned(vehicle)
        freeFailed = not free or (
            freeChannel.failingSince is not None
            and freeChannel.lastAttemptAt >= returnedAt
            and now - max(freeChannel.failingSince, returnedAt) >= PAID_AFTER[aspect]
            and now - freeChannel.lastAttemptAt < PROBE_AFTER
        )
        # A start goes with the limit first: its wake carries the limit
        startWillWake = aspect == CHARGE_LIMIT and self._startWanted(vehicle, config)
        field = FIELD_FOR[aspect]
        # Only a car expected to charge is woken; a restore or stop never is,
        # nor waits on the question
        wantsWake = not want.restore and want.value is not False
        quiet = whyQuiet(self.master, vehicle) if wantsWake else None
        if aspect == CHARGING and known(vehicle, CHARGING) is None:
            if key in self._wokeUnread or not self.wakeUnreadOnce:
                quiet = quiet or "its charging state is unknown"
        if aspect == CHARGING and quiet == "no room to start":
            # The start waits for room, keeping its desire and its budget
            return None
        wakes = wantsWake and quiet is None
        notSeenAwake = Presence.asleep(vehicle, rec.heldAt or 0) is not False
        # A billed provider never wakes for amps
        lastProvider = self.master.providerOrder.provider(rec.lastProvider)
        mayWake = wakes and (aspect != AMPS or getattr(lastProvider, "local", False))
        if not mayWake and last == O.ASLEEP and notSeenAwake:
            # Asleep (or offline): only a sign of life since the hold (the
            # cloud's next check may find one), or a message that may wake
            return None
        reached = rec.paidSends and reachedCar(
            vehicle, field, rec.paidAt, rec.beliefAtPaidSend
        )
        if (
            rec.paidSends
            and not rec.paidReached
            and not rec.rearmed
            and now - rec.paidAt >= RETRY[aspect]
            and (
                reached
                or startWillWake
                or self._chargingSince(vehicle, aspect, rec.paidAt)
                or (rec.paidSends >= PAID_SENDS_PER_ARM and rec.wakeCheckBy and wakes)
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
        paidAgain = rec.paidSends == 0
        if 0 < rec.paidSends < PAID_SENDS_PER_ARM:
            sincePaid = now - rec.paidAt
            retryDue = sincePaid >= RETRY[aspect] and (rec.paidRetryDue or reached)
            # Its wake failed: the follow-up through it (WAKE_CHECK)
            wakeCheckDue = (
                sincePaid >= WAKE_CHECK
                and wakes
                and paid
                and paid[0].name == rec.wakeCheckBy
            )
            paidAgain = retryDue or wakeCheckDue
        freeGap = RESTORE_RETRY if want.restore and rec.lastFreeAt else RETRY[aspect]
        # Spent only while the message would still not go
        spent = rec.paidSends + rec.paidSpent >= PAID_BUDGET
        if spent:
            verdict, _ = Presence.applicable(self.master, vehicle, capability)
            spent = verdict != Presence.SEND
        if paid and freeFailed and paidAgain and not spent:
            provider = paid[0]
            rec.beliefAtPaidSend = belief.value if belief is not None else None
        elif free and (now - rec.lastFreeAt >= freeGap or rec.lastFreeAt < returnedAt):
            provider = free[0]
        else:
            return None
        if aspect == CHARGING and want.value is True:
            limitWant = self.desired.get(key, CHARGE_LIMIT)
            limitRec = self.desired.record(key, CHARGE_LIMIT)
            write = vehicle.lastWrite(FIELD_FOR[CHARGE_LIMIT])
            if (
                limitRec.inFlight is not None
                or (write is not None and now - write.at < LIMIT_THEN_START_GAP)
                # Tried, unsettled and not spent
                or (
                    limitWant is not None
                    and limitRec.lastOutcome is not None
                    and not satisfied(
                        CHARGE_LIMIT, limitWant.value, known(vehicle, CHARGE_LIMIT)
                    )
                    and not self._limitSpent(vehicle, limitRec)
                )
            ):
                # A limit always goes first, and settles before a start
                return None
        # The blind retry never wakes and reads nothing: the first
        # attempt read. A paid attempt reads only a value older than its
        # whole window
        blind = provider in paid and rec.paidRetryDue
        wake = wakes and (provider.local or aspect != AMPS) and not blind
        attempt = Attempt(
            key,
            aspect,
            capability,
            want.value,
            provider.name,
            wake=wake,
            maxAge=(
                None
                if aspect == AMPS or blind
                else READ_MAX_AGE + (PAID_AFTER[aspect] if provider in paid else 0)
            ),
            restore=want.restore,
            # A desire set after this pass began is still this attempt's
            since=max(now, want.at),
            force=aspect == CHARGE_LIMIT
            and needsLiveRead(releases(want), belief.value if belief else None, saved),
            wakeSkipped=None if wake else self._whyNoWake(want, provider, quiet, blind),
            paidBefore=vehicle.paidRequests,
        )
        rec.inFlight = attempt
        return attempt

    @staticmethod
    def _whyNoWake(want, provider, quiet, blind) -> str:
        if blind:
            return "blind paid retry"
        if want.restore or want.value is False:
            return "restore or stop never wakes"
        return quiet or "billed provider never wakes for amps"

    def _startWanted(self, vehicle, config) -> bool:
        start = self.desired.get(vehicle.key, CHARGING)
        belief = known(vehicle, CHARGING, True)
        return (
            start is not None
            and start.value is True
            and not satisfied(CHARGING, True, belief)
            and whyHandsOff(vehicle, CHARGING, True, belief, config) is None
            # A start that would itself spend a cloud wake (not mode 2's)
            and any(
                not p.local and p.declares(C.WAKE)
                for p in self.master.providerOrder.candidates(vehicle, C.START_CHARGING)
            )
        )

    @staticmethod
    def _chargingSince(vehicle, aspect, since) -> bool:
        # A car drawing power is awake: a lowered limit gets one more send.
        # Not "Complete": a finished car may be asleep
        return aspect == CHARGE_LIMIT and any(
            obs.value in _DRAWING
            for obs in newerReadings(vehicle, FIELD_FOR[CHARGING], since)
        )

    def _limitSpent(self, vehicle, limitRec) -> bool:
        # The limit cannot be delivered, so it no longer holds the start:
        # every provider that can serve this car is excluded (a paired
        # BLE with its radio down still can: readiness is the claim);
        # both paid sends went to a car that answered; its one re-arm is
        # used with no blind retry left; a refusal holds it unsent; or the
        # car's limit could not be read twice, RETRY apart
        able = self.master.providerOrder.candidates(vehicle, C.SET_CHARGE_LIMIT)
        last = limitRec.lastOutcome
        return (
            all(p.name in limitRec.excluded for p in able)
            or (limitRec.paidSends >= PAID_SENDS_PER_ARM and limitRec.paidReached)
            or (limitRec.paidSends and limitRec.rearmed and not limitRec.paidRetryDue)
            or (last is not None and last.detail in _REFUSALS)
            or limitRec.unread >= 2
        )

    def _asleepCheckDue(self, vehicle, rec, now) -> bool:
        # A cloud hold's free online check runs again on the asleep cadence,
        # unless a live feed will report the car waking or the watch's own
        # cloud status checks refresh its sleep
        p = self.master.providerOrder.provider(rec.lastProvider)
        if rec.lastOutcome != O.ASLEEP or getattr(p, "local", True):
            return False
        if self.master.watch and Watch.cloudOnly(self.master, vehicle):
            return False
        home = Presence.presence(self.master, vehicle) == Presence.HOME
        gap = Presence.HOME_ASLEEP_SECS if home else Presence.AWAY_ASLEEP_SECS
        due = now - max(rec.lastOutcomeAt, rec.checkedAt) >= gap
        return due and not Presence.covered(self.master, vehicle)

    def _saved(self, vehicle):
        return self.master.heldLimits.get(vehicle.vin) if vehicle.vin else None

    def _save(self, vehicle, outside, held) -> None:
        # HeldLimits.set rounds floats (80.0 from Home Assistant)
        if vehicle.vin:
            self.master.heldLimits.set(vehicle.vin, outside=outside, held=held)

    # Hooks: runAttempt calls these on the queue's thread, around the command

    def inapplicable(self, vehicle, attempt) -> Optional[O.Outcome]:
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
            return O.Outcome(O.SATISFIED, O.HELD + why)
        self.desired.clearIf(attempt.key, attempt.aspect, attempt.value)
        return O.Outcome(O.SATISFIED, O.RULES + verdict)

    def beforeWrite(self, vehicle, attempt, provider, believed):
        # A charging point's own command acts on the point, which is here
        early = None if provider.module is None else self.inapplicable(vehicle, attempt)
        if early is not None or attempt.aspect != CHARGE_LIMIT:
            return attempt.value, early
        decision = decideChargeLimit(
            attempt.value,
            attempt.restore,
            believed.value if believed is not None else None,
            self._saved(vehicle),
        )
        if decision.saveNow is not None:
            self._save(vehicle, *decision.saveNow)
        if decision.settled:
            if attempt.restore:
                # Nothing left to restore: the desire is done
                self.desired.clearIf(attempt.key, CHARGE_LIMIT, attempt.value)
            return None, O.Outcome(O.SATISFIED, decision.reason)
        if decision.send is None:
            return None, O.Outcome(O.UNREACHABLE, decision.reason)
        # Only this worker touches it while the attempt is in flight
        self.desired.record(attempt.key, CHARGE_LIMIT).pendingSave = (
            decision.saveOnSuccess
        )
        return decision.send, None

    def afterWrite(self, vehicle, attempt, outcome) -> None:
        if attempt.aspect == CHARGE_LIMIT:
            rec = self.desired.record(attempt.key, CHARGE_LIMIT)
            save, rec.pendingSave = rec.pendingSave, None
            if save is not None and outcome.kind in O.SETTLED:
                self._save(vehicle, *save)
        elif (
            attempt.aspect == CHARGING
            and attempt.value is False
            and outcome.kind in O.SETTLED
        ):
            # A start goes back through the provider that stopped the car
            self.desired.state(attempt.key).lastStopProvider = attempt.provider

    def report(self, attempt, outcome) -> None:
        # On a queue's or the submitter's thread. Never submits or cancels
        if isinstance(attempt, Run.Job):
            if attempt.done is not None:
                attempt.done(outcome)
            return
        if isinstance(outcome, BaseException):
            # A queue's run raised past _run (SystemExit)
            outcome = O.Outcome(O.ERROR, type(outcome).__name__)
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
        if attempt.aspect == CHARGING and attempt.wake and not O.isNotSent(outcome):
            self._wokeUnread.add(attempt.key)
        provider = self.master.providerOrder.provider(attempt.provider)
        vehicle = self.master.vehicleRegistry.get(attempt.key)
        self.desired.noteOutcome(
            attempt.key,
            attempt.aspect,
            attempt,
            outcome,
            self._clock(),
            paid=_tier(provider, attempt.capability) >= C.TIER_BILLED,
            spent=vehicle.paidRequests - attempt.paidBefore if vehicle else 0,
        )
        self.nudge()
