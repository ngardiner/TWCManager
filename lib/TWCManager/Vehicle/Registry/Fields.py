# Per-vehicle field store: each field holds its latest observation (value,
# source, time) and the last value we wrote ourselves, so readers can tell a
# stale feed from a current one and an echo of our own write from news.
import threading
import time
from typing import Any, Callable, Dict, NamedTuple, Optional


class Observation(NamedTuple):
    value: Any
    source: str
    at: float


FIELDS = (
    "name",
    "batteryLevel",
    "chargeLimit",
    "chargingState",
    "pluggedIn",
    "state",
    "lat",
    "lon",
    "gpsAsOf",
    "actualCurrent",
    "availableCurrent",
    "voltage",
    "phases",
    "timeToFullCharge",
    "scheduledChargingPending",
    "twcVin",
    "bleReach",
)

# Source names are module class names. A source is report-on-change exactly
# when it has registered liveness with the registry; the store only sees the
# liveAt argument.
TELEMETRY_SOURCES = frozenset({"TeslaMateVehicle", "FleetTelemetryMQTT"})

# How long a healthy report-on-change feed gets to report a change after one
# of our own writes before its silence counts as "the write did not land".
FEED_LAG = 30.0

# Sentinel for age()/trusted(): the observation's source is polled, not a
# report-on-change feed.
NOT_TRACKED = object()


class FieldStore:
    # Also exposed on the class so callers can write FieldStore.NOT_TRACKED.
    NOT_TRACKED = NOT_TRACKED

    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        # Not reentrant: nothing may call back into the store while held.
        self._lock = threading.Lock()
        self._latest: Dict[str, Observation] = {}
        self._writes: Dict[str, Observation] = {}
        # Each source's own latest observation per field, so a report-on-change
        # feed can vouch for a field another source observed after it.
        self._bySource: Dict[str, Dict[str, Observation]] = {}

    @staticmethod
    def _check(field: str) -> None:
        if field not in FIELDS:
            raise KeyError(field)

    def _store(
        self,
        table: Dict[str, Observation],
        field: str,
        value: Any,
        source: str,
        at: Optional[float],
        bySource: Optional[Dict[str, Dict[str, Observation]]] = None,
    ) -> Observation:
        # The clock is read under the lock so stored order matches stamp
        # order. An older stamp never displaces a newer one (equal replaces);
        # the entry that stays is returned. bySource, when given, keeps the
        # same rule per source.
        with self._lock:
            obs = Observation(value, source, self._clock() if at is None else at)
            if bySource is not None:
                mine = bySource.setdefault(field, {})
                prev = mine.get(source)
                if prev is None or obs.at >= prev.at:
                    mine[source] = obs
            old = table.get(field)
            if old is not None and obs.at < old.at:
                return old
            table[field] = obs
            return obs

    def observe(
        self, field: str, value: Any, source: str, at: Optional[float] = None
    ) -> Observation:
        self._check(field)
        return self._store(self._latest, field, value, source, at, self._bySource)

    def reported(self, field: str) -> Dict[str, Observation]:
        """Each source's latest observation of field."""
        with self._lock:
            return dict(self._bySource.get(field, {}))

    def latest(self, field: str) -> Optional[Observation]:
        with self._lock:
            return self._latest.get(field)

    def noteWrite(
        self, field: str, value: Any, source: str, at: Optional[float] = None
    ) -> Observation:
        self._check(field)
        return self._store(self._writes, field, value, source, at)

    def lastWrite(self, field: str) -> Optional[Observation]:
        with self._lock:
            return self._writes.get(field)

    def age(
        self, field: str, now: Optional[float] = None, liveAt: Any = NOT_TRACKED
    ) -> Optional[float]:
        obs = self.latest(field)
        if obs is None:
            return None
        if now is None:
            now = self._clock()
        if liveAt is NOT_TRACKED or liveAt is None:
            return now - obs.at
        # A live feed's liveAt can be a moment past now; age is never negative.
        return max(0.0, now - max(obs.at, liveAt))

    @staticmethod
    def _fresher(obs: Optional[Observation], write: Optional[Observation]) -> bool:
        if write is None:
            return True
        # Strictly newer: an observation stamped at the instant of our own
        # write is not trusted over it.
        return obs is not None and obs.at > write.at

    def fresherThanWrite(self, field: str) -> bool:
        with self._lock:
            return self._fresher(self._latest.get(field), self._writes.get(field))

    def trusted(
        self,
        field: str,
        now: Optional[float] = None,
        liveAt: Any = NOT_TRACKED,
        obs: Optional[Observation] = None,
    ) -> Optional[Observation]:
        # obs, when given, is judged in place of the latest observation (a
        # caller that leaves some sources out passes its own pick).
        if now is None:
            now = self._clock()
        with self._lock:
            if obs is None:
                obs = self._latest.get(field)
            write = self._writes.get(field)
        if obs is None:
            return None
        if self._fresher(obs, write):
            return obs
        # Healthy report-on-change feed that had time to report a change and
        # did not: our write did not land, so its value stands.
        if (
            liveAt is not NOT_TRACKED
            and liveAt is not None
            and now - write.at > FEED_LAG
        ):
            return obs
        return None

    def snapshot(self) -> Dict[str, Observation]:
        with self._lock:
            return dict(self._latest)
