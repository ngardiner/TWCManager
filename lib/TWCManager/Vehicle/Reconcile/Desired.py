# Desired state per car and aspect, the attempt record the reconciler keeps
# for each, per-provider channel health and the session bounds a user's own
# changes set. Decision-makers write here; the reconciler converges the car
# on it. Every stamp is the registry's clock domain (time.time). The store's
# lock is a leaf: it is never held while calling a provider, the registry,
# settings or a change callback.
import logging
import threading
import time
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Set

from ..Providers.Capability import (
    SET_AMPS,
    SET_CHARGE_LIMIT,
    START_CHARGING,
    STOP_CHARGING,
)
from .Outcome import REFUSED, Outcome

logger = logging.getLogger(__name__.split(".")[0] + ".Vehicle.Reconcile")

# Aspects
CHARGE_LIMIT = "chargeLimit"
CHARGING = "charging"
AMPS = "amps"
ASPECTS = frozenset({CHARGE_LIMIT, CHARGING, AMPS})
# A user change to a slow aspect replaces desired until the next decision; a
# user change to a fast aspect becomes a session bound
SLOW_ASPECTS = frozenset({CHARGE_LIMIT})

# The registry field each aspect is observed in. AMPS is the car's own
# current request, never availableCurrent (the charger's pilot)
FIELD_FOR = {
    CHARGE_LIMIT: "chargeLimit",
    CHARGING: "chargingState",
    AMPS: "chargeCurrentRequest",
}

# Charging states that mean the car is charging or about to (Task 6's
# satisfied(CHARGING, True, value) supersedes this when it exists)
CHARGING_LIKE = ("Charging", "Starting", "Complete")
STOPPED_LIKE = ("Stopped",)


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


_RECONCILER = __name__.rsplit(".", 1)[0] + ".Reconciler"


def _chargingLike(value) -> bool:
    # Lazy: Reconciler imports this module. Only a missing Reconciler module
    # falls back; any other import failure is a real error and propagates.
    # Never called under the store's lock (a first import reads files)
    try:
        from .Reconciler import satisfied
    except ModuleNotFoundError as e:
        if e.name != _RECONCILER:
            raise
        return value is True or value in CHARGING_LIKE
    return bool(satisfied(CHARGING, True, value))


def _stoppedLike(value) -> bool:
    return value is False or value in STOPPED_LIKE


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
    # A stop the wiring or a controller limit requires, which a user-start
    # bound does not override
    forced: bool = False


class Attempt(NamedTuple):
    key: str
    # None for a probe or a one-shot debug command
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
    def __init__(self):
        self.lastOutcome: Optional[Outcome] = None
        self.lastOutcomeAt: float = 0
        self.lastProvider: str = ""
        self.reset()

    def reset(self) -> None:
        # Everything but the last outcome, its time and its provider
        self.inFlight: Optional[Attempt] = None
        # Failures per provider this round
        self.tried: Dict[str, int] = {}
        self.excluded: Set[str] = set()
        # The excluded providers that REFUSED (retired their slot or claim);
        # the rest were UNSUPPORTED, which may depend on the value
        self.refused: Set[str] = set()
        # Billed or disruptive providers tried this round
        self.paid: Set[str] = set()
        # 0 until the first exhaustion
        self.backoff: float = 0
        self.backoffUntil: float = 0
        self.settledAt: Optional[float] = None
        # An unstamped settled outcome (a disruptive stop, a SATISFIED
        # start/stop) may be re-planned no sooner than this
        self.notBefore: float = 0
        self.pendingSave: Any = None

    def newDesire(self) -> None:
        # A new value gets a prompt free attempt; a paid provider's tries and
        # the backoff persist until success or a session event
        self.tried = {k: v for k, v in self.tried.items() if k in self.paid}
        # UNSUPPORTED can depend on the value (amps outside a range); a
        # refusal does not
        self.excluded = self.excluded & self.refused
        self.notBefore = 0
        if not self.paid:
            self.backoff = 0
            self.backoffUntil = 0

    def newRound(self) -> None:
        # paid stays, so a backed-off round still knows it was paid
        self.tried = {}
        self.excluded = set()
        self.refused = set()

    def exclude(self, provider: str, kind: str) -> None:
        # Skip the provider for the rest of the round (an EXCLUDING outcome).
        # An exclusion added to excluded directly counts as UNSUPPORTED
        self.excluded.add(provider)
        if kind == REFUSED:
            self.refused.add(provider)

    def summary(self) -> dict:
        last = self.lastOutcome
        return {
            "inFlight": _attemptSummary(self.inFlight),
            "lastOutcome": (
                {"kind": last.kind, "detail": last.detail} if last else None
            ),
            "lastOutcomeAt": self.lastOutcomeAt,
            "lastProvider": self.lastProvider,
            "tried": dict(self.tried),
            "excluded": sorted(self.excluded),
            "refused": sorted(self.refused),
            "paid": sorted(self.paid),
            "backoff": self.backoff,
            "backoffUntil": self.backoffUntil,
            "settledAt": self.settledAt,
            "notBefore": self.notBefore,
        }


class ChannelHealth:
    # Per car and free provider, shared by every aspect
    def __init__(self):
        self.reset()

    def reset(self) -> None:
        self.lastSuccessAt: Optional[float] = None
        self.failingSince: Optional[float] = None
        self.lastFailureAt: Optional[float] = None

    def success(self, at: float) -> None:
        self.lastSuccessAt = at
        self.failingSince = None

    def failure(self, at: float) -> None:
        self.failingSince = self.failingSince or at
        self.lastFailureAt = at

    @property
    def failing(self) -> bool:
        return self.failingSince is not None

    def summary(self) -> dict:
        return {
            "lastSuccessAt": self.lastSuccessAt,
            "failingSince": self.failingSince,
            "lastFailureAt": self.lastFailureAt,
            "failing": self.failing,
        }


class SessionBounds:
    def __init__(self):
        self.clear()

    def clear(self) -> None:
        # The user lowered the car's current request this session
        self.maxAmps: Optional[float] = None
        # The user started charging: no non-forced stop this session
        self.wantsCharge: bool = False
        # The user stopped charging: no start this session
        self.userStopped: bool = False
        self.lifted: bool = False

    def summary(self) -> dict:
        return {
            "maxAmps": self.maxAmps,
            "wantsCharge": self.wantsCharge,
            "userStopped": self.userStopped,
            "lifted": self.lifted,
        }


class ControlState:
    def __init__(self):
        self.desired: Dict[str, Desired] = {}
        self.records: Dict[str, AttemptRecord] = {}
        self.bounds = SessionBounds()
        # By provider name
        self.health: Dict[str, ChannelHealth] = {}
        # At most one BLE reachability probe per car
        self.probeInFlight: Optional[Attempt] = None
        # The provider that delivered the last CHARGING False; a start goes
        # back through it
        self.lastStopProvider: Optional[str] = None
        # None until the point reports; True when it offered at least its
        # minimum on the last cycle
        self.offering: Optional[bool] = None
        self.offeringSince: float = 0


def _desiredSummary(desired: Desired) -> dict:
    return {
        "value": desired.value,
        "by": desired.by,
        "at": desired.at,
        "adopted": desired.adopted,
        "restore": desired.restore,
        "lift": desired.lift,
        "forced": desired.forced,
    }


class DesiredStore:
    # Keys are registry keys (a VIN or an anon:... key), so the per-key maps
    # are bounded by the registry; per-key maps are bounded by ASPECTS and
    # the registered provider names.
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
        self,
        key,
        aspect,
        value,
        by,
        decision=False,
        restore=False,
        lift=False,
        forced=False,
    ) -> bool:
        _checkAspect(aspect)
        ignored = None
        changed = False
        with self._lock:
            state = self._state(key)
            existing = state.desired.get(aspect)
            bounds = state.bounds
            if existing is not None and existing.adopted and not decision:
                ignored = "the user's value holds until the next decision"
            elif (
                aspect == CHARGING
                and value is False
                and bounds.wantsCharge
                and not forced
            ):
                ignored = "the user started charging this session"
            elif aspect == CHARGING and value is True and bounds.userStopped:
                ignored = "the user stopped charging this session"
            elif (
                existing is not None
                and existing.value == value
                and (existing.restore, existing.lift, existing.forced)
                == (restore, lift, forced)
            ):
                # Unchanged (a different by alone is not a new desire). A
                # decision on an adopted value takes it over without a new
                # attempt: the car already has it
                if existing.adopted:
                    state.desired[aspect] = existing._replace(adopted=False, by=by)
            else:
                state.desired[aspect] = Desired(
                    value, by, self._clock(), False, restore, lift, forced
                )
                self._record(state, aspect).newDesire()
                changed = True
        if ignored:
            logger.debug(
                "Desired %s %s=%r by %s ignored: %s", key, aspect, value, by, ignored
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

    def release(self, key, aspect, value, by) -> bool:
        # Drops a pending desire that never went out (a distributor stop
        # cancelled before delivery); a delivered one stays
        _checkAspect(aspect)
        with self._lock:
            state = self._states.get(key)
            existing = state.desired.get(aspect) if state else None
            removed = (
                existing is not None
                and existing.value == value
                and existing.by == by
                and not self._settled(state, aspect)
            )
            if removed:
                del state.desired[aspect]
        if removed:
            self._notify(key, (aspect,))
        return removed

    def adopt(self, key, aspect, value) -> None:
        # The car reports a value the user set. A slow aspect takes it as
        # desired until the next decision; a fast aspect turns it into a
        # session bound and drops desired. The caller decides whether a read
        # is the user's doing (Task 8: a stopped read is adopted only while
        # the point is offering and has been since before the read).
        _checkAspect(aspect)
        # Classified before the lock: the rule may import Reconciler
        chargingLike = aspect == CHARGING and _chargingLike(value)
        stoppedLike = aspect == CHARGING and _stoppedLike(value)
        changed = value is not None
        with self._lock:
            state = self._state(key)
            if not changed:
                # An unread value is never the user's
                pass
            elif aspect in SLOW_ASPECTS:
                state.desired[aspect] = Desired(
                    value, "user", self._clock(), adopted=True
                )
            elif aspect == CHARGING:
                if chargingLike:
                    state.bounds.wantsCharge = True
                    state.bounds.userStopped = False
                    state.desired.pop(aspect, None)
                elif stoppedLike:
                    state.bounds.userStopped = True
                    state.bounds.wantsCharge = False
                    state.desired.pop(aspect, None)
                else:
                    changed = False
            else:
                state.bounds.maxAmps = value
                state.desired.pop(aspect, None)
        if changed:
            logger.debug("Adopted %s %s=%r", key, aspect, value)
            self._notify(key, (aspect,))
        else:
            logger.debug("Not adopting %s %s=%r", key, aspect, value)

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

    def endSession(self, key) -> None:
        with self._lock:
            state = self._states.get(key)
            if state is None:
                return
            state.bounds.clear()
            for aspect, desired in list(state.desired.items()):
                if desired.adopted:
                    state.desired[aspect] = desired._replace(adopted=False)
            for record in state.records.values():
                record.reset()
            # In place, so a held reference sees the reset
            for health in state.health.values():
                health.reset()
            state.lastStopProvider = None
        self._notify(key, sorted(ASPECTS))

    def noteOffering(self, key, offering: bool) -> None:
        # Whether the point was offered at least its minimum this cycle
        offering = bool(offering)
        with self._lock:
            state = self._state(key)
            started = offering and not state.offering
            changed = state.offering is not offering
            state.offering = offering
            if started:
                state.offeringSince = self._clock()
        if changed:
            self._notify(key, (CHARGING,))

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

    def health(self, key, provider) -> ChannelHealth:
        with self._lock:
            state = self._state(key)
            health = state.health.get(provider)
            if health is None:
                health = ChannelHealth()
                state.health[provider] = health
            return health

    def state(self, key) -> ControlState:
        with self._lock:
            return self._state(key)

    def bounds(self, key) -> SessionBounds:
        with self._lock:
            return self._state(key).bounds

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
                "bounds": state.bounds.summary(),
                "health": {
                    name: health.summary() for name, health in state.health.items()
                },
                "probeInFlight": _attemptSummary(state.probeInFlight),
                "lastStopProvider": state.lastStopProvider,
                "offering": state.offering,
                "offeringSince": state.offeringSince,
            }
