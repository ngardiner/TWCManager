# One vehicle in the registry: identity (VIN, or an anonymous plug session),
# provider claims, observed/managed tier, attachments and a summary.
import logging
import threading
import time
from typing import Any, Callable, Collection, Dict, List, Optional, Tuple, Union

from .Fields import NOT_TRACKED, TELEMETRY_SOURCES, FieldStore, Observation

logger = logging.getLogger(__name__.split(".")[0] + ".Vehicle.Registry")


class Claim:
    def __init__(
        self,
        provider: str,
        canCommand: Union[bool, Callable[[], bool]],
        status: str = "",
        since: float = 0.0,
        keepsAlive: bool = True,
    ):
        self.provider = provider
        self.canCommand = canCommand
        self.status = status
        self.since = since
        # False for a speculative claim (an unproven BLE key): it must not
        # keep an unseen car in the registry past the forget window.
        self.keepsAlive = keepsAlive

    @property
    def commands(self) -> bool:
        c = self.canCommand
        if not callable(c):
            return bool(c)
        try:
            return bool(c())
        except Exception:
            logger.debug("Claim %s canCommand raised; counted as False", self.provider)
            return False


class Vehicle:
    # Telemetry (an external module such as TeslaMate) is the sync source
    # while its latest "state" observation is younger than this. Age is
    # liveness-aware: on a connected, healthy feed a car that has sent
    # nothing for hours (asleep) stays telemetry-sourced.
    SYNC_TIMEOUT = 60 * 60

    def __init__(
        self,
        key: str,
        vin: Optional[str],
        registry,
        clock: Callable[[], float] = time.time,
        userAsserted: bool = False,
    ):
        self.key = key
        self.vin = vin
        self.isAnonymous = vin is None
        self.userAsserted = userAsserted
        self._registry = registry
        self._clock = clock
        now = clock()
        self.firstSeen = now
        self.lastSeen = now
        self.lastSeenSource = "created"
        self._fields = FieldStore(clock)
        # Leaf lock: guards claims, attachments and lastSeen only.
        self._lock = threading.Lock()
        self._claims: Dict[str, Claim] = {}
        self._attachments: Dict[str, Any] = {}

    # Fields

    def observe(
        self,
        field: str,
        value: Any,
        source: str,
        at: Optional[float] = None,
        seen: bool = True,
    ) -> Observation:
        obs = self._fields.observe(field, value, source, at)
        if seen:
            with self._lock:
                # A future stamp must not push lastSeen ahead of now.
                at = min(obs.at, self._clock())
                if at >= self.lastSeen:
                    self.lastSeen = at
                    self.lastSeenSource = obs.source
        return obs

    def latest(self, field: str) -> Optional[Observation]:
        return self._fields.latest(field)

    def reported(self, field: str) -> Dict[str, Observation]:
        """Each source's own latest observation of field."""
        return self._fields.reported(field)

    def lastWrite(self, field: str) -> Optional[Observation]:
        return self._fields.lastWrite(field)

    def latestFrom(
        self, field: str, exclude: Collection[str] = ()
    ) -> Optional[Observation]:
        """The newest observation of field from a source not in exclude."""
        latest = self._fields.latest(field)
        if latest is None or latest.source not in exclude:
            return latest
        best = None
        for source, obs in self._fields.reported(field).items():
            if source not in exclude and (best is None or obs.at > best.at):
                best = obs
        return best

    def believed(
        self, field: str, exclude: Collection[str] = ()
    ) -> Optional[Observation]:
        """The value to act on: a read strictly newer than our last write,
        else our last write, else the read."""
        obs = self.latestFrom(field, exclude)
        write = self._fields.lastWrite(field)
        if obs is not None and (write is None or obs.at > write.at):
            return obs
        return write if write is not None else obs

    def _liveAt(
        self, field: str, obs: Optional[Observation], exclude: Collection[str] = ()
    ) -> Any:
        if obs is None or self._registry is None:
            return NOT_TRACKED
        live = self._registry.sourceLiveAt(obs.source, self)
        # A report-on-change feed sends nothing while a value holds, so a
        # healthy feed that has reported this field vouches for it even after
        # another source observed it, whatever value that source saw: the
        # feed would have reported a change. Requiring equal values made a
        # parked car whose cloud lat differs from TeslaMate's in the last
        # digit cost a paid read per window after one feed outage.
        for source in self._fields.reported(field):
            if source == obs.source or source in exclude:
                continue
            other = self._registry.sourceLiveAt(source, self)
            if other is NOT_TRACKED or other is None:
                continue
            live = other if live is NOT_TRACKED or live is None else max(live, other)
        return live

    def age(self, field: str) -> Optional[float]:
        # now is read before liveness: a live feed's liveAt is "now", and a
        # later now would make its value a hair old and miss maxAge 0.
        now = self._clock()
        obs = self._fields.latest(field)
        return self._fields.age(field, now=now, liveAt=self._liveAt(field, obs))

    def trusted(
        self, field: str, exclude: Collection[str] = ()
    ) -> Optional[Observation]:
        obs = self.latestFrom(field, exclude)
        if obs is None:
            # Never fall back to the store's latest: it may be excluded.
            return None
        return self._fields.trusted(
            field, liveAt=self._liveAt(field, obs, exclude), obs=obs
        )

    # Sync state is the latest "state" observation's source and age.

    def telemetryState(self) -> Tuple[Optional[Observation], Optional[float]]:
        obs = self.latest("state")
        if obs is None or obs.source not in TELEMETRY_SOURCES:
            return obs, None
        return obs, self.age("state")

    @property
    def syncSource(self) -> str:
        # The module keeping the car current, never shown: the TeslaAPI
        # module (either slot) when no telemetry feed is live
        obs, age = self.telemetryState()
        if age is not None and age <= self.SYNC_TIMEOUT:
            return obs.source
        return "TeslaAPI"

    def value(self, field: str, default: Any = None) -> Any:
        obs = self._fields.latest(field)
        return default if obs is None else obs.value

    def noteWrite(self, field: str, value: Any, source: str) -> Observation:
        return self._fields.noteWrite(field, value, source)

    def read(
        self,
        field: str,
        maxAge: Optional[float],
        allowWake: bool = False,
        providers: Optional[Collection[Any]] = None,
        force: bool = False,
    ) -> Optional[Observation]:
        # providers (Provider objects or names) limits the provider order's
        # read cascade to those, in cost order. force skips the freshness
        # check: a live report-on-change feed keeps age() at 0, which a
        # restore must not trust. maxAge None still never fetches.
        latest = self.latest(field)
        if maxAge is None or self._registry is None:
            return latest
        if latest is not None and not force:
            age = self.age(field)
            # A value not newer than our own write (inside the feed lag) is
            # not fresh: it may predate the write.
            if age is not None and age <= maxAge and self.trusted(field):
                return latest
        order = getattr(self._registry, "providerOrder", None)
        # No lock is held here: providers may do I/O.
        got = order.read(self, field, allowWake, providers) if order else None
        return got if got is not None else self.latest(field)

    # Claims

    def claim(
        self,
        provider: str,
        canCommand: Union[bool, Callable[[], bool]],
        status: str = "",
        keepsAlive: bool = True,
    ) -> Claim:
        c = Claim(provider, canCommand, status, self._clock(), keepsAlive)
        with self._lock:
            self._claims[provider] = c
        return c

    def dropClaim(self, provider: str) -> bool:
        with self._lock:
            return self._claims.pop(provider, None) is not None

    @property
    def claims(self) -> Dict[str, Claim]:
        with self._lock:
            return dict(self._claims)

    # Attachments

    def attach(self, name: str, obj: Any) -> None:
        with self._lock:
            self._attachments[name] = obj

    def attachment(self, name: str) -> Optional[Any]:
        with self._lock:
            return self._attachments.get(name)

    def attachmentNames(self) -> List[str]:
        with self._lock:
            return sorted(self._attachments)

    @property
    def name(self) -> str:
        obs = self._fields.latest("name")
        return obs.value if obs is not None and obs.value else ""

    def summary(self) -> dict:
        now = self._clock()
        claims: List[dict] = [
            {"provider": c.provider, "status": c.status, "commands": c.commands}
            for c in self.claims.values()
        ]
        if self.isAnonymous:
            point = self.key[5:]
        else:
            point = self.value("twcVin")
        with self._lock:
            lastSeen, lastSeenSource = self.lastSeen, self.lastSeenSource
        fields = {}
        for f, o in self._fields.snapshot().items():
            liveAt = self._liveAt(f, o)
            fields[f] = {
                "value": o.value,
                "source": o.source,
                "age": self._fields.age(f, now=now, liveAt=liveAt),
                "changedAge": now - o.at,
            }
        return {
            "key": self.key,
            "vin": self.vin,
            "name": self.name,
            "anonymous": self.isAnonymous,
            "userAsserted": self.userAsserted,
            "claims": claims,
            "point": point,
            "lastSeen": lastSeen,
            "lastSeenAge": now - lastSeen,
            "lastSeenSource": lastSeenSource,
            "fields": fields,
        }
