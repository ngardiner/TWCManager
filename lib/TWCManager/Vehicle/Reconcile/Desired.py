# Desired state per car and aspect, the attempt record the reconciler keeps
# for each and one free-channel record per car. Decision-makers write here;
# the reconciler converges the car on it. Only the charge limit is adopted
# from the car; charging and amps are stateless. Every stamp is the
# registry's clock domain (time.time). The store's lock is a leaf: it is
# never held while calling a provider, the registry, settings, a worker or a
# change callback.
import logging
import threading
import time
from typing import Any, Callable, Dict, List, NamedTuple, Optional

from ..Providers.Capability import (
    SET_AMPS,
    SET_CHARGE_LIMIT,
    START_CHARGING,
    STOP_CHARGING,
)
from .Outcome import (
    EXCLUDING,
    LANE_DETAILS,
    REFUSED,
    RETRYABLE,
    SETTLED,
    UNSUPPORTED,
    Outcome,
    isNotSent,
)

logger = logging.getLogger(__name__.split(".")[0] + ".Vehicle.Reconcile")

# Aspects
CHARGE_LIMIT = "chargeLimit"
CHARGING = "charging"
AMPS = "amps"
ASPECTS = frozenset({CHARGE_LIMIT, CHARGING, AMPS})

# The registry field each aspect is observed in. AMPS is the car's own
# current request, never availableCurrent (the charger's pilot)
FIELD_FOR = {
    CHARGE_LIMIT: "chargeLimit",
    CHARGING: "chargingState",
    AMPS: "chargeCurrentRequest",
}


def CAPABILITY_FOR(aspect, value) -> str:
    if aspect == CHARGE_LIMIT:
        return SET_CHARGE_LIMIT
    if aspect == CHARGING:
        return START_CHARGING if value is True else STOP_CHARGING
    if aspect == AMPS:
        return SET_AMPS
    raise ValueError("unknown aspect %r" % (aspect,))


def _checkAspect(aspect) -> None:
    if aspect not in ASPECTS:
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
    # local-amps rule (#660)
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


def _attemptSummary(attempt: Optional[Attempt]) -> Optional[dict]:
    if attempt is None:
        return None
    return {
        "capability": attempt.capability,
        "value": attempt.value,
        "provider": attempt.provider,
        "wake": attempt.wake,
        "since": attempt.since,
    }


class AttemptRecord:
    # Mutated after submit only through DesiredStore.noteOutcome
    def __init__(self):
        self.inFlight: Optional[Attempt] = None
        self.lastOutcome: Optional[Outcome] = None
        self.lastOutcomeAt: float = 0
        self.lastProvider: str = ""
        # Provider -> the EXCLUDING kind that excluded it
        self.excluded: Dict[str, str] = {}
        # The last request any provider sent, and the last a free one sent
        self.lastSentAt: float = 0
        self.lastFreeAt: float = 0
        # The paid or disruptive request for the current desire
        self.paidAt: Optional[float] = None
        # That request failed and its one blind retry is unused
        self.paidRetryDue: bool = False
        self.settledAt: Optional[float] = None
        self.pendingSave: Any = None

    def newDesire(self) -> None:
        # UNSUPPORTED can depend on the value (amps outside a range); a
        # refusal does not
        self.excluded = {p: k for p, k in self.excluded.items() if k != UNSUPPORTED}
        self.lastFreeAt = 0
        self.paidAt = None
        self.paidRetryDue = False
        self.settledAt = None

    def exclude(self, provider: str, kind: str) -> None:
        self.excluded[provider] = kind

    def summary(self) -> dict:
        last = self.lastOutcome
        return {
            "inFlight": _attemptSummary(self.inFlight),
            "lastOutcome": (
                {"kind": last.kind, "detail": last.detail} if last else None
            ),
            "lastOutcomeAt": self.lastOutcomeAt,
            "lastProvider": self.lastProvider,
            "excluded": dict(self.excluded),
            "lastSentAt": self.lastSentAt,
            "lastFreeAt": self.lastFreeAt,
            "paidAt": self.paidAt,
            "paidRetryDue": self.paidRetryDue,
            "settledAt": self.settledAt,
        }


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

    def summary(self) -> dict:
        return {"failingSince": self.failingSince, "lastAttemptAt": self.lastAttemptAt}


class ControlState:
    def __init__(self):
        self.desired: Dict[str, Desired] = {}
        self.records: Dict[str, AttemptRecord] = {}
        self.free = FreeChannel()
        # The provider that delivered the last CHARGING False; a start goes
        # back through it
        self.lastStopProvider: Optional[str] = None


def _desiredSummary(desired: Desired) -> dict:
    return {
        "value": desired.value,
        "by": desired.by,
        "at": desired.at,
        "adopted": desired.adopted,
        "restore": desired.restore,
        "lift": desired.lift,
    }


class DesiredStore:
    # Keys are registry keys (a VIN or an anon:... key), so the per-key maps
    # are bounded by the registry; per-key maps are bounded by ASPECTS and
    # the registered provider names. Never call Workers.submit or cancel
    # while holding the lock (Run.py's header).
    def __init__(self, clock=time.time):
        self._clock = clock
        self._lock = threading.Lock()
        self._states: Dict[str, ControlState] = {}
        self._callbacks: List[Callable[[str, str], None]] = []

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

    def _settled(self, state, aspect) -> bool:
        desired = state.desired.get(aspect)
        record = state.records.get(aspect)
        if desired is None or record is None or record.settledAt is None:
            return False
        return record.settledAt >= desired.at

    def _notify(self, key, aspects) -> None:
        # Outside the lock
        callbacks = list(self._callbacks)
        for aspect in aspects:
            for callback in callbacks:
                try:
                    callback(key, aspect)
                except Exception as e:
                    logger.debug(
                        "Desired-state change callback raised %s", type(e).__name__
                    )

    # --- writes ---

    def set(
        self, key, aspect, value, by, decision=False, restore=False, lift=False
    ) -> bool:
        _checkAspect(aspect)
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
                self._record(state, aspect).newDesire()
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
            self._notify(key, (aspect,))
        return changed

    def clear(self, key, aspect) -> bool:
        _checkAspect(aspect)
        with self._lock:
            state = self._states.get(key)
            removed = state is not None and state.desired.pop(aspect, None) is not None
        if removed:
            self._notify(key, (aspect,))
        return removed

    def clearIf(self, key, aspect, value) -> bool:
        # Clears only a desire for this value (a pending stop cancelled, a
        # start no longer offered)
        _checkAspect(aspect)
        with self._lock:
            state = self._states.get(key)
            existing = state.desired.get(aspect) if state else None
            removed = existing is not None and existing.value == value
            if removed:
                del state.desired[aspect]
        if removed:
            self._notify(key, (aspect,))
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
        self._notify(key, (aspect,))

    def noteOutcome(
        self,
        key,
        aspect,
        attempt,
        outcome,
        now,
        paid: bool,
        freeProvider: bool,
    ) -> bool:
        # The only change to a record after submit. A report for anything
        # but the attempt in flight is stale and changes nothing
        _checkAspect(aspect)
        with self._lock:
            state = self._states.get(key)
            record = state.records.get(aspect) if state else None
            if record is None or record.inFlight is not attempt:
                return False
            record.inFlight = None
            if outcome.detail in LANE_DETAILS:
                # The attempt never ran
                return True
            record.lastOutcome = outcome
            record.lastOutcomeAt = now
            record.lastProvider = attempt.provider
            if not isNotSent(outcome):
                record.lastSentAt = now
                if freeProvider:
                    record.lastFreeAt = now
                if paid:
                    # One blind retry, after the desire's first paid request
                    # failed; never after a failed wake
                    record.paidRetryDue = (
                        record.paidAt is None
                        and outcome.kind in RETRYABLE
                        and not outcome.detail.startswith("wake ")
                    )
                    record.paidAt = now
            if outcome.kind in SETTLED:
                record.settledAt = now
                if freeProvider:
                    state.free.success(now)
            elif freeProvider and (
                (outcome.kind in RETRYABLE and outcome.detail != "pairing")
                or outcome.kind == REFUSED
            ):
                state.free.failure(now)
            if outcome.kind in EXCLUDING:
                record.exclude(attempt.provider, outcome.kind)
            return True

    def forget(self, key) -> bool:
        # The registry dropped the vehicle: every trace goes, no callback
        with self._lock:
            return self._states.pop(key, None) is not None

    def followRegistry(self, registry) -> None:
        # Forget a key whenever the registry drops it (forget, expiry, an
        # anonymous plug session ending or merging into a VIN)
        registry.onRemove(self.forget)

    def decision(self, key) -> None:
        # A new decision: the next set applies over adopted values
        with self._lock:
            state = self._states.get(key)
            if state is None:
                return
            for aspect, desired in list(state.desired.items()):
                if desired.adopted:
                    state.desired[aspect] = desired._replace(adopted=False)

    def onChange(self, callback) -> None:
        with self._lock:
            self._callbacks = self._callbacks + [callback]

    # --- reads ---

    def get(self, key, aspect) -> Optional[Desired]:
        _checkAspect(aspect)
        with self._lock:
            state = self._states.get(key)
            return state.desired.get(aspect) if state else None

    def record(self, key, aspect) -> AttemptRecord:
        _checkAspect(aspect)
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

    def settled(self, key, aspect) -> bool:
        _checkAspect(aspect)
        with self._lock:
            state = self._states.get(key)
            return state is not None and self._settled(state, aspect)

    def keys(self) -> List[str]:
        with self._lock:
            return list(self._states)

    def summary(self, key) -> dict:
        with self._lock:
            state = self._states.get(key)
            if state is None:
                return {}
            return {
                "desired": {
                    aspect: _desiredSummary(desired)
                    for aspect, desired in state.desired.items()
                },
                "records": {
                    aspect: record.summary() for aspect, record in state.records.items()
                },
                "free": state.free.summary(),
                "lastStopProvider": state.lastStopProvider,
            }
