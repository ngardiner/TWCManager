# The registry of vehicles: membership, forgetting, persistence and the
# read-source and liveness tables. Registry and vehicle locks are leaves:
# nothing here calls a source, a claim callable or does I/O while holding one.
import logging
import math
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from .Fields import NOT_TRACKED
from .ReadSource import ReadSource
from .Vehicle import Vehicle
from .VIN import isWellFormedVIN

logger = logging.getLogger(__name__.split(".")[0] + ".Vehicle.Registry")

ANON_PREFIX = "anon:"


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
        self._sources: List[ReadSource] = []
        self._liveness: Dict[str, Callable[[Vehicle], Optional[float]]] = {}
        # Per point: {"candidate": vin, "attempts": int, "rejected": bool}.
        self._pending: Dict[str, dict] = {}

    @classmethod
    def forMaster(cls, master) -> "VehicleRegistry":
        registry = getattr(master, "vehicleRegistry", None)
        if isinstance(registry, cls):
            return registry
        config = getattr(master, "config", None)
        return cls(config if isinstance(config, dict) else {})

    # Membership

    @staticmethod
    def _normalise(key: Any) -> Any:
        if isinstance(key, str) and not key.startswith(ANON_PREFIX):
            return key.upper()
        return key

    def _getOrCreate(self, vin: str, source: str):
        # load() applies the same rule, so what toJSON wrote always reloads.
        if (
            not isinstance(vin, str)
            or not vin
            or not vin.isascii()
            or vin.startswith(ANON_PREFIX)
        ):
            raise ValueError("a VIN is required")
        vin = vin.upper()
        with self._lock:
            v = self._vehicles.get(vin)
            if v is not None:
                return v, False
            v = Vehicle(vin, vin, self, clock=self._clock)
            self._vehicles[vin] = v
        # A load restores known cars: not news.
        level = logging.DEBUG if source == "persisted" else logging.INFO
        logger.log(level, "Vehicle %s added (%s)", vin, source)
        return v, True

    def getOrCreate(self, vin: str, source: str) -> Vehicle:
        return self._getOrCreate(vin, source)[0]

    def get(self, key: str) -> Optional[Vehicle]:
        with self._lock:
            return self._vehicles.get(self._normalise(key))

    def vehicles(self) -> List[Vehicle]:
        with self._lock:
            return [self._vehicles[k] for k in sorted(self._vehicles)]

    def findByName(self, name: str) -> Optional[Vehicle]:
        for v in self.vehicles():
            if v.name == name:
                return v
        return None

    def attached(self, name: str) -> List[Any]:
        found = []
        for v in self.vehicles():
            a = v.attachment(name)
            if a is not None:
                found.append(a)
        return found

    def addUserAsserted(self, vin: str) -> Vehicle:
        if not isinstance(vin, str) or not isWellFormedVIN(vin):
            raise ValueError("VIN must be 17 characters without I, O or Q")
        v = self.getOrCreate(vin, "user")
        v.userAsserted = True
        return v

    def forget(self, key: str) -> bool:
        with self._lock:
            removed = self._vehicles.pop(self._normalise(key), None)
        if removed is not None:
            logger.info("Vehicle %s forgotten", removed.key)
        return removed is not None

    def expire(self, now: Optional[float] = None) -> List[str]:
        if now is None:
            now = self._clock()
        gone = []
        with self._lock:
            for key in sorted(self._vehicles):
                v = self._vehicles[key]
                if v.isAnonymous or v.userAsserted or v.claims:
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
        return gone

    # Plug sessions and TWC VIN confirmation. Called from the serial thread:
    # dict work under the leaf lock only, logging and vehicle writes outside.

    def beginPlugSession(self, pointId: str) -> Vehicle:
        key = ANON_PREFIX + pointId
        with self._lock:
            v = self._vehicles.get(key)
            if v is None:
                v = Vehicle(key, None, self, clock=self._clock)
                self._vehicles[key] = v
            return v

    def endPlugSession(self, pointId: str) -> None:
        key = ANON_PREFIX + pointId
        with self._lock:
            v = self._vehicles.get(key)
            if v is not None and v.isAnonymous:
                del self._vehicles[key]
            self._pending.pop(pointId, None)
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
            # Older stamps never displace newer ones, so the car's own
            # newer values survive the merge.
            for field, obs in anon._fields.snapshot().items():
                v.observe(field, obs.value, obs.source, at=obs.at)
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

    def pointFor(self, vin: str) -> Optional[str]:
        v = self.get(vin)
        return None if v is None else v.value("twcVin")

    def vehicleAt(self, pointId: str) -> Optional[Vehicle]:
        attributed = self._attributedTo(pointId)
        if attributed:
            return attributed[0]
        return self.get(ANON_PREFIX + pointId)

    # Read sources and liveness

    def registerReadSource(self, source: ReadSource) -> None:
        with self._lock:
            if source not in self._sources:
                self._sources.append(source)

    def readSources(self) -> List[ReadSource]:
        with self._lock:
            indexed = list(enumerate(self._sources))
        return [
            s
            for _, s in sorted(indexed, key=lambda p: (getattr(p[1], "order", 0), p[0]))
        ]

    def registerLiveness(
        self, source: str, liveAt: Callable[[Vehicle], Optional[float]]
    ) -> None:
        with self._lock:
            self._liveness[source] = liveAt

    def sourceLiveAt(self, source: str, vehicle: Vehicle) -> Any:
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
                # Same acceptance as getOrCreate, so what toJSON wrote loads.
                if not isinstance(vin, str) or not vin.isascii():
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

    def summaries(self) -> List[dict]:
        return [v.summary() for v in self.vehicles()]
