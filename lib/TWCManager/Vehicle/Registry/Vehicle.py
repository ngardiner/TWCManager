# One vehicle in the registry: identity (VIN, or an anonymous plug session),
# provider claims, observed/managed tier, attachments and a summary.
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Union

from .Fields import NOT_TRACKED, FieldStore, Observation
from .ReadSource import cascade

logger = logging.getLogger(__name__.split(".")[0] + ".Vehicle.Registry")


class Claim:
    def __init__(
        self,
        provider: str,
        canCommand: Union[bool, Callable[[], bool]],
        status: str = "",
        since: float = 0.0,
    ):
        self.provider = provider
        self.canCommand = canCommand
        self.status = status
        self.since = since

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

    def reportedBy(self, field: str, source: str) -> Optional[Observation]:
        """source's own latest observation of field."""
        return self._fields.reported(field).get(source)

    def lastWrite(self, field: str) -> Optional[Observation]:
        return self._fields.lastWrite(field)

    def _liveAt(self, field: str, obs: Optional[Observation]) -> Any:
        if obs is None or self._registry is None:
            return NOT_TRACKED
        live = self._registry.sourceLiveAt(obs.source, self)
        # A report-on-change feed sends nothing while a value holds, so once
        # another source re-observes the value the feed last reported, the
        # feed's liveness still vouches for it. A different value is not
        # vouched for: it ages until a source confirms it. Floats compare
        # exactly: TeslaMate republishes the API's own lat/lon digits, and a
        # mismatch only costs a fetch.
        for source, mine in self._fields.reported(field).items():
            if source == obs.source or mine.value != obs.value:
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

    def trusted(self, field: str) -> Optional[Observation]:
        obs = self._fields.latest(field)
        return self._fields.trusted(field, liveAt=self._liveAt(field, obs))

    def value(self, field: str, default: Any = None) -> Any:
        obs = self._fields.latest(field)
        return default if obs is None else obs.value

    def noteWrite(self, field: str, value: Any, source: str) -> Observation:
        return self._fields.noteWrite(field, value, source)

    def fresherThanWrite(self, field: str) -> bool:
        return self._fields.fresherThanWrite(field)

    def read(
        self, field: str, maxAge: Optional[float], allowWake: bool = False
    ) -> Optional[Observation]:
        latest = self.latest(field)
        if maxAge is None or self._registry is None:
            return latest
        if latest is not None:
            age = self.age(field)
            # A value not newer than our own write (inside the feed lag) is
            # not fresh: it may predate the write.
            if age is not None and age <= maxAge and self.trusted(field):
                return latest
        # No lock is held here: sources may do I/O.
        got = cascade(self, self._registry.readSources(), field, allowWake)
        return got if got is not None else self.latest(field)

    # Claims and tier

    def claim(
        self,
        provider: str,
        canCommand: Union[bool, Callable[[], bool]],
        status: str = "",
    ) -> Claim:
        c = Claim(provider, canCommand, status, self._clock())
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

    @property
    def tier(self) -> str:
        # Callables run outside the lock: they may call into other modules.
        return self._tierOf([c.commands for c in self.claims.values()])

    def _tierOf(self, commands: List[bool]) -> str:
        return "managed" if not self.isAnonymous and any(commands) else "observed"

    # Attachments

    def attach(self, name: str, obj: Any) -> None:
        with self._lock:
            self._attachments[name] = obj

    def attachment(self, name: str) -> Optional[Any]:
        with self._lock:
            return self._attachments.get(name)

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
        tier = self._tierOf([c["commands"] for c in claims])
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
            "tier": tier,
            "claims": claims,
            "point": point,
            "lastSeen": lastSeen,
            "lastSeenAge": now - lastSeen,
            "lastSeenSource": lastSeenSource,
            "fields": fields,
        }
