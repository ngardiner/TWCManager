# The registry of vehicles: membership, forgetting, persistence and the
# liveness table. Registry and vehicle locks are leaves: nothing here calls a
# provider, a claim callable or does I/O while holding one.
import logging
import math
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from .Fields import NOT_TRACKED
from .Vehicle import Vehicle
from .VIN import isWellFormedVIN

logger = logging.getLogger(__name__.split(".")[0] + ".Vehicle.Registry")

ANON_PREFIX = "anon:"


def isAnonKey(key) -> bool:
    return isinstance(key, str) and key.startswith(ANON_PREFIX)


def canonicalKey(key):
    # VINs compare upper-case; an anonymous key is kept as issued
    return key.upper() if isinstance(key, str) and not isAnonKey(key) else key


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


class VehicleRegistry:
    DEFAULT_FORGET_DAYS = 30
    CONFIRM_ATTEMPTS = 3

    def __init__(self, config: dict, clock: Callable[[], float] = time.time):
        vehicleConfig = (config or {}).get("vehicle", {})
        days = _number(
            vehicleConfig.get("forgetAfterDays", self.DEFAULT_FORGET_DAYS)
            if isinstance(vehicleConfig, dict)
            else None
        )
        if days is None or days <= 0:
            logger.warning(
                "vehicle.forgetAfterDays must be a positive number; using %d",
                self.DEFAULT_FORGET_DAYS,
            )
            days = self.DEFAULT_FORGET_DAYS
        self.forgetAfter = days * 86400
        self._clock = clock
        self._lock = threading.RLock()
        self._vehicles: Dict[str, Vehicle] = {}
        # The ProviderOrder a stale read cascades through; None never fetches
        self.providerOrder = None
        self._liveness: Dict[str, Callable[[Optional[Vehicle]], Optional[float]]] = {}
        # Per point: {"candidate": vin, "attempts": int, "rejected": bool}.
        self._pending: Dict[str, dict] = {}
        # Called with a key, outside the lock, whenever a vehicle leaves:
        # state kept elsewhere per key (desired state) follows the removals
        self.removed: Optional[Callable[[str], None]] = None
        # (vehicle, field, obs, the source's prior obs), after each store
        self.onObserve: Optional[Callable[..., None]] = None

    @classmethod
    def forMaster(cls, master) -> "VehicleRegistry":
        registry = getattr(master, "vehicleRegistry", None)
        if isinstance(registry, cls):
            return registry
        config = getattr(master, "config", None)
        return cls(config if isinstance(config, dict) else {})

    def now(self) -> float:
        """The registry's clock: observation stamps and ages use it."""
        return self._clock()

    # Membership

    def _getOrCreate(self, vin: str, source: str, attach=None):
        # The one gate for every provider: only a well-formed VIN becomes a
        # vehicle (anonymous vehicles come from beginPlugSession). attach
        # (name, obj) lands under the lock, so a forget sees it or no vehicle
        if not isinstance(vin, str) or not isWellFormedVIN(vin):
            raise ValueError("a VIN is required")
        vin = vin.upper()
        with self._lock:
            v = self._vehicles.get(vin)
            created = v is None
            if created:
                v = self._vehicles[vin] = Vehicle(vin, vin, self, clock=self._clock)
            if attach is not None:
                v.attach(*attach)
        if created:
            # A load restores known cars: not news.
            level = logging.DEBUG if source == "persisted" else logging.INFO
            logger.log(level, "Vehicle %s added (%s)", vin, source)
        return v, created

    def getOrCreate(self, vin: str, source: str, attach=None) -> Vehicle:
        return self._getOrCreate(vin, source, attach)[0]

    def get(self, key: str) -> Optional[Vehicle]:
        with self._lock:
            return self._vehicles.get(canonicalKey(key))

    def vehicles(self) -> List[Vehicle]:
        with self._lock:
            return [self._vehicles[k] for k in sorted(self._vehicles)]

    def findByName(self, name: str) -> Optional[Vehicle]:
        for v in self.vehicles():
            if v.name == name:
                return v
        return None

    def attached(self, name: str) -> List[Tuple[Vehicle, Any]]:
        # (vehicle, attachment) for each vehicle with one, in registry order
        pairs = [(v, v.attachment(name)) for v in self.vehicles()]
        return [(v, a) for v, a in pairs if a is not None]

    def addUserAsserted(self, vin: str) -> Vehicle:
        if not isinstance(vin, str) or not isWellFormedVIN(vin):
            raise ValueError("VIN must be 17 characters without I, O or Q")
        v = self.getOrCreate(vin, "user")
        v.userAsserted = True
        return v

    def _removed(self, keys: List[str]) -> None:
        # Outside the lock
        for key in keys:
            try:
                if self.removed is not None:
                    self.removed(key)
            except Exception as e:
                logger.debug("Vehicle removal callback raised %s", type(e).__name__)

    def forget(self, key: str, ifUnattached: bool = False) -> bool:
        # ifUnattached: only a user-asserted vehicle no provider holds, checked
        # under the lock a listing attaches under
        key = canonicalKey(key)
        with self._lock:
            v = self._vehicles.get(key)
            if ifUnattached and v is not None:
                if not v.userAsserted or v.attachmentNames():
                    return False
            removed = self._vehicles.pop(key, None)
        if removed is not None:
            logger.info("Vehicle %s forgotten", removed.key)
            self._removed([removed.key])
        return removed is not None

    def expire(self, now: Optional[float] = None) -> List[str]:
        if now is None:
            now = self._clock()
        gone = []
        with self._lock:
            for key in sorted(self._vehicles):
                v = self._vehicles[key]
                # An attachment is a provider's listing: kept, as its claims
                if v.isAnonymous or v.userAsserted or v.attachmentNames():
                    continue
                if any(c.keepsAlive for c in v.claims.values()):
                    continue
                if now - v.lastSeen > self.forgetAfter:
                    del self._vehicles[key]
                    gone.append(key)
        for key in gone:
            logger.info(
                "Vehicle %s forgotten: not seen for %d days",
                key,
                int(self.forgetAfter // 86400),
            )
        self._removed(gone)
        return gone

    # Plug sessions and TWC VIN confirmation. Called from the serial thread:
    # dict work under the leaf lock only, logging and vehicle writes outside.

    def beginPlugSession(self, pointId: str) -> Vehicle:
        key = ANON_PREFIX + pointId
        with self._lock:
            v = self._vehicles.get(key)
            if v is None:
                v = Vehicle(key, None, self, clock=self._clock)
                v.mark("plugIn", self.now())
                self._vehicles[key] = v
            return v

    def endPlugSession(self, pointId: str) -> None:
        key = ANON_PREFIX + pointId
        removed = False
        with self._lock:
            v = self._vehicles.get(key)
            if v is not None and v.isAnonymous:
                del self._vehicles[key]
                removed = True
            self._pending.pop(pointId, None)
        if removed:
            self._removed([key])
        for v in self._attributedTo(pointId):
            v.observe("twcVin", None, "TWC", seen=False)

    def observeTwcVin(self, pointId: str, vin: str) -> str:
        if not isinstance(vin, str) or not isWellFormedVIN(vin):
            return "rejected"
        vin = vin.upper()
        rejectedNow = False
        with self._lock:
            if vin in self._vehicles:
                status = "confirmed"
            else:
                state = self._pending.setdefault(
                    pointId, {"candidate": None, "attempts": 0, "rejected": False}
                )
                if state["rejected"]:
                    return "rejected"
                state["attempts"] += 1
                if state["candidate"] == vin:
                    status = "confirmed"
                elif state["attempts"] >= self.CONFIRM_ATTEMPTS:
                    state["rejected"] = rejectedNow = True
                    status = "rejected"
                else:
                    state["candidate"] = vin
                    status = "pending"
            if status == "confirmed":
                self._pending.pop(pointId, None)
        if rejectedNow:
            logger.info("TWC %s VIN %s not confirmed; ignoring", pointId, vin)
        if status != "confirmed":
            return status
        v = self.getOrCreate(vin, "TWC")
        key = ANON_PREFIX + pointId
        with self._lock:
            anon = self._vehicles.pop(key, None)
        if anon is not None:
            self._removed([key])
            # Older stamps never displace newer ones, so the car's own
            # newer values survive the merge.
            for field, obs in anon.snapshot().items():
                v.observe(field, obs.value, obs.source, at=obs.at, seen=obs.seen)
        # One car per point: the one it names now
        source = "Gen3TWC" if pointId.startswith("Gen3TWC-") else "Gen2TWC"
        for other in self._attributedTo(pointId):
            if other is not v:
                other.observe("twcVin", None, "TWC", seen=False)
                # Nor plugged in there, if it was guessed to be
                if getattr(other.reported("pluggedIn").get(source), "value", 0):
                    other.observe("pluggedIn", False, source, seen=False)
        v.observe("twcVin", pointId, "TWC")
        return "confirmed"

    def isPending(self, pointId: str) -> bool:
        # True while the point has an unconfirmed, not-yet-rejected candidate.
        with self._lock:
            state = self._pending.get(pointId)
            return (
                state is not None
                and state["candidate"] is not None
                and not state["rejected"]
            )

    def _attributedTo(self, pointId: str) -> List[Vehicle]:
        found = []
        for v in self.vehicles():
            if v.isAnonymous:
                continue
            obs = v.latest("twcVin")
            if obs is not None and obs.value == pointId:
                found.append((obs.at, v))
        found.sort(key=lambda p: p[0], reverse=True)
        return [v for _, v in found]

    def vehicleAt(self, pointId: str) -> Optional[Vehicle]:
        attributed = self._attributedTo(pointId)
        if attributed:
            return attributed[0]
        return self.get(ANON_PREFIX + pointId)

    # Liveness

    def registerLiveness(
        self, source: str, liveAt: Callable[[Optional[Vehicle]], Optional[float]]
    ) -> None:
        with self._lock:
            self._liveness[source] = liveAt

    def sourceLiveAt(self, source: str, vehicle: Optional[Vehicle] = None) -> Any:
        # vehicle None asks about the feed as a whole (e.g. its connection).
        with self._lock:
            liveAt = self._liveness.get(source)
        if liveAt is None:
            return NOT_TRACKED
        try:
            # Anything but a finite number counts as not live.
            return _number(liveAt(vehicle))
        except Exception as e:
            logger.debug("Liveness for %s raised %s", source, type(e).__name__)
            return None

    # Persistence

    def toJSON(self) -> dict:
        out = {}
        for v in self.vehicles():
            if v.isAnonymous:
                continue
            with v._lock:
                lastSeen, lastSeenSource = v.lastSeen, v.lastSeenSource
            out[v.vin] = {
                "name": v.name,
                "firstSeen": v.firstSeen,
                "lastSeen": lastSeen,
                "lastSeenSource": lastSeenSource,
                "userAsserted": bool(v.userAsserted),
            }
        return {"version": 1, "vehicles": out}

    def load(self, data: Any) -> None:
        if not isinstance(data, dict) or not isinstance(data.get("vehicles"), dict):
            return
        now = self._clock()
        for vin, entry in data["vehicles"].items():
            try:
                if not isinstance(entry, dict):
                    continue
                # Same acceptance as getOrCreate: an entry an older release
                # persisted under a malformed key is dropped, not raised.
                if not isinstance(vin, str) or not isWellFormedVIN(vin):
                    continue
                self._loadOne(vin, entry, now)
            except Exception as e:
                logger.warning(
                    "Ignoring unreadable persisted vehicle (%s)", type(e).__name__
                )

    def _loadOne(self, vin: str, entry: dict, now: float) -> None:
        first = _number(entry.get("firstSeen"))
        last = _number(entry.get("lastSeen"))
        # Persisted times never run ahead of now.
        first = now if first is None else min(first, now)
        last = now if last is None else min(last, now)
        first = min(first, last)
        source = entry.get("lastSeenSource")
        source = source if isinstance(source, str) and source else "persisted"
        v, created = self._getOrCreate(vin, "persisted")
        with v._lock:
            if created:
                v.firstSeen, v.lastSeen, v.lastSeenSource = first, last, source
            else:
                v.firstSeen = min(v.firstSeen, first)
                if last > v.lastSeen:
                    v.lastSeen, v.lastSeenSource = last, source
        if entry.get("userAsserted") is True:
            v.userAsserted = True
        name = entry.get("name")
        if isinstance(name, str) and name:
            v.observe("name", name, "persisted", at=last, seen=False)
