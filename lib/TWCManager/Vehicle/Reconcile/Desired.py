# Desired state per car and aspect, with the attempt record the reconciler
# keeps for each and one free-channel record per car. Only the charge limit
# is adopted from the car; charging and amps are stateless. The store's lock
# is a leaf: never held while calling a provider, the registry, settings, a
# queue or changed.
import logging
import threading
import time
from typing import Any, Callable, Dict, List, NamedTuple, Optional

from ..Providers import Capability as C
from . import Outcome as O

logger = logging.getLogger(__name__.split(".")[0] + ".Vehicle.Reconcile")

# Aspects
CHARGE_LIMIT = "chargeLimit"
CHARGING = "charging"
AMPS = "amps"
# The charge limit of a policy with none: the car's own (outside) limit
RELEASE = -1

# The registry field each aspect is observed in. AMPS is the car's own
# current request, never availableCurrent (the charger's pilot)
FIELD_FOR = {
    CHARGE_LIMIT: "chargeLimit",
    CHARGING: "chargingState",
    AMPS: "chargeCurrentRequest",
}


def CAPABILITY_FOR(aspect, value) -> str:
    if aspect == CHARGE_LIMIT:
        return C.SET_CHARGE_LIMIT
    if aspect == CHARGING:
        return C.START_CHARGING if value is True else C.STOP_CHARGING
    if aspect == AMPS:
        return C.SET_AMPS
    raise ValueError("unknown aspect %r" % (aspect,))


class Desired(NamedTuple):
    value: Any
    by: str
    at: float
    # The user's own value, adopted from a read; held until the next decision
    adopted: bool = False
    # The value is the outside limit being put back
    restore: bool = False
    # A raise-to-max of the car's own current limit, which ignores the
    # local-amps rule
    lift: bool = False


class Attempt(NamedTuple):
    key: str
    # None for a one-shot debug command
    aspect: Optional[str]
    capability: str
    value: Any
    provider: str
    wake: bool
    maxAge: Optional[float]
    restore: bool
    since: float
    # The pre-write read cascades even when the stored value is fresh
    force: bool = False
    # Why a command goes without a wake (logged by runAttempt)
    wakeSkipped: Optional[str] = None
    # The car's paid request count when planned (Vehicle.paidRequests)
    paidBefore: int = 0


class AttemptRecord:
    # Mutated after submit only through DesiredStore.noteOutcome

    # What a new desire resets (all immutable, so shared). The NOT_SENT hold
    # (lastOutcome) belongs to the desire it came from
    _FRESH = dict(
        lastOutcome=None,
        lastOutcomeAt=0,
        lastFreeAt=0,  # the last request a free provider sent
        # The paid or disruptive requests for the current desire, the last
        # one's stamp and the value believed when it was planned
        paidSends=0,
        paidReached=False,  # one of them got an answer other than UNREACHABLE
        paidAt=None,
        paidSaw=None,
        paidRetryDue=False,  # that request failed and its one blind retry is unused
        rearmed=False,  # the desire's one re-arm of its paid sends is used
        # The provider whose wake the car did not answer on the last paid
        # send: the command never went (Reconciler's WAKE_CHECK)
        wakeCheckBy=None,
        settledAt=None,
        heldAt=None,  # a hold: when, and the paid requests attempts that held made
        paidSpent=0,
        unread=0,  # sends that found the current value unreadable (a limit's backstop)
        clearedValue=None,  # the value a clearIf withdrew; set() may take the record back
    )

    def __init__(self):
        # What spans desires: the send gap and the attempt in flight
        self.inFlight: Optional[Attempt] = None
        self.lastProvider: str = ""
        # Provider -> the EXCLUDING kind that excluded it
        self.excluded: Dict[str, str] = {}
        # The last request any provider sent
        self.lastSentAt: float = 0
        self.pendingSave: Any = None
        self.__dict__.update(self._FRESH)

    def newDesire(self) -> None:
        # Not inFlight, lastProvider, lastSentAt or pendingSave: plan reads
        # inFlight without the lock, and the send gap spans desires
        self.__dict__.update(self._FRESH)
        # UNSUPPORTED can depend on the value (amps outside a range); a
        # refusal does not
        self.excluded = {p: k for p, k in self.excluded.items() if k != O.UNSUPPORTED}

    def exclude(self, provider: str, kind: str) -> None:
        self.excluded[provider] = kind


class FreeChannel:
    # One per car, shared by every aspect and free provider
    def __init__(self):
        self.failingSince: Optional[float] = None
        self.lastAttemptAt: float = 0

    def success(self, at: float) -> None:
        self.lastAttemptAt = at
        self.failingSince = None

    def failure(self, at: float) -> None:
        # Never "or": 0.0 is a valid stamp
        if self.failingSince is None:
            self.failingSince = at
        self.lastAttemptAt = at


class ControlState:
    def __init__(self):
        self.desired: Dict[str, Desired] = {}
        self.records: Dict[str, AttemptRecord] = {}
        self.free = FreeChannel()
        # The provider that delivered the last CHARGING False; a start goes
        # back through it
        self.lastStopProvider: Optional[str] = None


class DesiredStore:
    # Keys are registry keys (a VIN or an anon:... key), so the per-key maps
    # are bounded by the registry and the three aspects. Never submit or
    # cancel while holding the lock (Reconciler.py's header).
    def __init__(self, clock=time.time):
        self._clock = clock
        self._lock = threading.Lock()
        self._states: Dict[str, ControlState] = {}
        # Called with (key, aspect), outside the lock, on every change
        self.changed: Optional[Callable[[str, str], None]] = None

    # --- internals (callers hold the lock) ---

    def _state(self, key) -> ControlState:
        state = self._states.get(key)
        if state is None:
            state = ControlState()
            self._states[key] = state
        return state

    def _record(self, state, aspect) -> AttemptRecord:
        record = state.records.get(aspect)
        if record is None:
            record = AttemptRecord()
            state.records[aspect] = record
        return record

    def _notify(self, key, aspect) -> None:
        # Outside the lock
        try:
            if self.changed is not None:
                self.changed(key, aspect)
        except Exception as e:
            logger.debug("Desired-state change callback raised %s", type(e).__name__)

    # --- writes ---

    def set(
        self,
        key,
        aspect,
        value,
        by,
        decision=False,
        restore=False,
        lift=False,
        keep=False,
    ) -> bool:
        # keep: a value clearIf withdrew comes back on its old record, so
        # its paid budget is not spent again
        ignored = False
        changed = False
        with self._lock:
            state = self._state(key)
            existing = state.desired.get(aspect)
            if existing is not None and existing.adopted and not decision:
                # The user's value holds until the next decision
                ignored = True
            elif (
                existing is not None
                and existing.value == value
                and (existing.restore, existing.lift) == (restore, lift)
            ):
                # Unchanged (a different by alone is not a new desire). A
                # decision on an adopted value takes it over without a new
                # attempt: the car already has it
                if existing.adopted:
                    state.desired[aspect] = existing._replace(adopted=False, by=by)
            else:
                state.desired[aspect] = Desired(
                    value, by, self._clock(), False, restore, lift
                )
                record = self._record(state, aspect)
                if not (keep and existing is None and record.clearedValue == value):
                    record.newDesire()
                changed = True
        if ignored:
            logger.debug(
                "Desired %s %s=%r by %s ignored: the user's value holds",
                key,
                aspect,
                value,
                by,
            )
        if changed:
            self._notify(key, aspect)
        return changed

    def clear(self, key, aspect) -> bool:
        with self._lock:
            state = self._states.get(key)
            removed = state is not None and state.desired.pop(aspect, None) is not None
        if removed:
            self._notify(key, aspect)
        return removed

    def clearIf(self, key, aspect, value) -> bool:
        # Clears only a desire for this value (a pending stop cancelled, a
        # start no longer offered)
        with self._lock:
            state = self._states.get(key)
            existing = state.desired.get(aspect) if state else None
            removed = existing is not None and existing.value == value
            if removed:
                del state.desired[aspect]
                self._record(state, aspect).clearedValue = value
        if removed:
            self._notify(key, aspect)
        return removed

    def adopt(self, key, aspect, value) -> None:
        # The car reports a charge limit the user set: it is desired until
        # the next decision. Charging and amps are never adopted
        if aspect != CHARGE_LIMIT:
            raise ValueError("only %s is adopted, not %r" % (CHARGE_LIMIT, aspect))
        if value is None:
            # An unread value is never the user's
            return
        with self._lock:
            self._state(key).desired[aspect] = Desired(
                value, "user", self._clock(), adopted=True
            )
        logger.debug("Adopted %s %s=%r", key, aspect, value)
        self._notify(key, aspect)

    def noteOutcome(
        self,
        key,
        aspect,
        attempt,
        outcome,
        now,
        paid: bool,
        spent: int = 0,
    ) -> bool:
        # The only change to a record after submit. A report for anything
        # but the attempt in flight is stale and changes nothing; so is a
        # debug one-shot's (no aspect)
        if aspect is None:
            return False
        with self._lock:
            state = self._states.get(key)
            record = state.records.get(aspect) if state else None
            if record is None or record.inFlight is not attempt:
                return False
            if outcome.detail in O.QUEUE_DETAILS:
                # The attempt never ran
                record.inFlight = None
                return True
            want = state.desired.get(aspect)
            # Only an attempt made for the current desire settles it or
            # spends its budget; any attempt feeds the free channel
            current = (
                want is not None
                and want.value == attempt.value
                and attempt.since >= want.at
            )
            held = outcome.detail.startswith(O.HELD)
            if current:
                # A hold sent nothing: the desire waits for newer evidence
                record.heldAt = now if held else None
                # The paid requests an attempt that held made (wake, read)
                record.paidSpent += spent if held else 0
                record.lastOutcome = outcome
                record.lastOutcomeAt = now
                record.lastProvider = attempt.provider
                record.unread += outcome.detail.startswith("rules: could not read")
            sent = not O.isNotSent(outcome)
            if sent and outcome.kind not in O.EXCLUDING:
                # Even for a replaced desire: a start still in flight when
                # the desire flips spaces the stop. An excluding outcome
                # changed nothing on the car: the fallback need not wait
                record.lastSentAt = now
            if current and sent:
                if not paid:
                    record.lastFreeAt = now
                else:
                    # One blind retry, after the desire's first paid request
                    # failed; never after a failed wake
                    retry = outcome.kind in O.RETRYABLE
                    failedWake = retry and outcome.detail.startswith("wake ")
                    record.paidRetryDue = (
                        record.paidSends == 0 and retry and not failedWake
                    )
                    record.wakeCheckBy = attempt.provider if failedWake else None
                    record.paidSends += 1
                    # A failed wake (an HTTP error too) never reached the car
                    record.paidReached |= (
                        outcome.kind != O.UNREACHABLE and not failedWake
                    )
                    record.paidAt = now
            if current and outcome.kind in O.SETTLED and not held:
                record.settledAt = now
            if paid or held:
                pass
            elif outcome.kind in O.SETTLED or outcome.detail == "pairing":
                # Inside the pairing window the car answers, refusing the key
                # until the card is tapped: the channel works, and the paid
                # fallback waits
                state.free.success(now)
            elif sent and (
                # A failure of the free channel needs a request to it
                outcome.kind in O.RETRYABLE
                or outcome.kind == O.REFUSED
            ):
                state.free.failure(now)
            if outcome.kind == O.REFUSED or (current and outcome.kind in O.EXCLUDING):
                # UNSUPPORTED can depend on the value: a superseded one's not
                record.exclude(attempt.provider, outcome.kind)
            # Last: plan reads the record without the lock once this is None
            record.inFlight = None
            return True

    def forget(self, key) -> bool:
        # The registry dropped the vehicle: every trace goes, no callback
        with self._lock:
            return self._states.pop(key, None) is not None

    # --- reads ---

    def get(self, key, aspect) -> Optional[Desired]:
        with self._lock:
            state = self._states.get(key)
            return state.desired.get(aspect) if state else None

    def record(self, key, aspect) -> AttemptRecord:
        with self._lock:
            return self._record(self._state(key), aspect)

    def free(self, key) -> FreeChannel:
        with self._lock:
            return self._state(key).free

    def state(self, key) -> ControlState:
        with self._lock:
            return self._state(key)

    def peek(self, key) -> Optional[ControlState]:
        # A read that never creates an entry (readiness, summaries)
        with self._lock:
            return self._states.get(key)

    def keys(self) -> List[str]:
        with self._lock:
            return list(self._states)
