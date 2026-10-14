# One vehicle in the registry: identity (VIN, or an anonymous plug session),
# provider claims, observed/managed tier, attachments and a summary.
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Union

from .Fields import NOT_TRACKED, FieldStore, Observation


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
        return bool(c() if callable(c) else c)


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
                if obs.at >= self.lastSeen:
                    self.lastSeen = obs.at
                    self.lastSeenSource = obs.source
        return obs

    def latest(self, field: str) -> Optional[Observation]:
        return self._fields.latest(field)

    def _liveAt(self, obs: Optional[Observation]) -> Any:
        if obs is None or self._registry is None:
            return NOT_TRACKED
        return self._registry.sourceLiveAt(obs.source, self)

    def age(self, field: str) -> Optional[float]:
        obs = self._fields.latest(field)
        return self._fields.age(field, liveAt=self._liveAt(obs))

    def trusted(self, field: str) -> Optional[Observation]:
        obs = self._fields.latest(field)
        return self._fields.trusted(field, liveAt=self._liveAt(obs))

    def value(self, field: str, default: Any = None) -> Any:
        obs = self._fields.latest(field)
        return default if obs is None else obs.value

    def noteWrite(self, field: str, value: Any, source: str) -> Observation:
        return self._fields.noteWrite(field, value, source)

    def fresherThanWrite(self, field: str) -> bool:
        return self._fields.fresherThanWrite(field)

    def read(self, field: str, maxAge: float, allowWake: bool = False):
        raise NotImplementedError("Vehicle.read arrives with the read cascade")

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
        if self.isAnonymous:
            return "observed"
        # Callables run outside the lock: they may call into other modules.
        return (
            "managed" if any(c.commands for c in self.claims.values()) else "observed"
        )

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
        if self.isAnonymous:
            point = self.key[5:]
        else:
            point = self.value("twcVin")
        with self._lock:
            lastSeen, lastSeenSource = self.lastSeen, self.lastSeenSource
        fields = {
            f: {"value": o.value, "source": o.source, "age": now - o.at}
            for f, o in self._fields.snapshot().items()
        }
        return {
            "key": self.key,
            "vin": self.vin,
            "name": self.name,
            "anonymous": self.isAnonymous,
            "userAsserted": self.userAsserted,
            "tier": self.tier,
            "claims": claims,
            "point": point,
            "lastSeen": lastSeen,
            "lastSeenAge": now - lastSeen,
            "lastSeenSource": lastSeenSource,
            "fields": fields,
        }
