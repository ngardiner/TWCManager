# The wall-power segments of cars whose charge readings arrive only as
# occasional cloud snapshots. Each snapshot's battery energy says how long
# and how hard the car drew between readings, so the history can be rebuilt
# after the fact; an open session stands in for the time since the last
# reading at whatever the caller believes the car draws now
import copy
import threading
import time
from typing import Callable, NamedTuple, Optional

CHARGING = "Charging"
UNPLUGGED = "Disconnected"
JOULES_PER_KWH = 3.6e6


class Snapshot(NamedTuple):
    stamp: float
    state: str
    energy: Optional[float]  # kWh added this session, battery side
    watts: float  # wall AC; 0 when not charging


class ChargeLedger:
    # Battery energy -> wall energy, measured 0.91-0.98 against a TWC
    EFFICIENCY = 0.93
    # The counter drifts down a little once charging stops (0.12 kWh seen
    # overnight). Headroom over that also bounds what an unplug no reading
    # saw can lose: a new plug-in's first reading this close is taken as a resume
    DRIFT_KWH = 0.5
    KEEP_SECS = 2 * 86400

    def __init__(self, clock=time.time):
        self.clock = clock
        # One leaf lock: state is copied out under it, and callbacks run after
        self.lock = threading.Lock()
        self.cars = {}

    def _car(self, vin):
        return self.cars.setdefault(
            vin, {"open": None, "segments": [], "seen": None, "energy": None}
        )

    def _wall(self, kwh, secs):
        return kwh * JOULES_PER_KWH / self.EFFICIENCY / secs

    def _write(self, car, start, end, watts):
        if end > start and watts > 0:
            car["segments"].append([start, end, watts])

    def _open(self, car, snap, startHint, last, ceiling=0.0):
        # The session's energy so far spans back to its start, found from the
        # load step if one was seen, else from the energy at the fastest rate
        # the car could have drawn: the reading's own rate may follow a
        # slowdown, and would date the start hours early.
        # The counter resets only on unplugging: a charge resumed on the same
        # plug-in added only what it grew past the last reading. Nothing reads
        # the car once a session closes, so the counter may since have drifted
        # below that reading. Either way the start never reaches into the
        # car's last segment
        car["open"] = {"stamp": snap.stamp, "energy": snap.energy, "watts": snap.watts}
        if snap.energy is None:
            return
        if last is not None and snap.energy >= last - self.DRIFT_KWH:
            energy = max(0.0, snap.energy - last)
        else:
            energy = snap.energy
        rate = max(snap.watts, ceiling)
        if startHint is not None and startHint <= snap.stamp:
            start = startHint
        elif rate > 0:
            start = snap.stamp - energy * JOULES_PER_KWH / (rate * self.EFFICIENCY)
        else:
            return
        if car["segments"]:
            start = max(start, car["segments"][-1][1])
        if snap.stamp > start:
            self._write(car, start, snap.stamp, self._wall(energy, snap.stamp - start))

    def _close(self, car, snap):
        # Energy added after the last Charging reading bounds how long it
        # kept charging; the closing reading's own stamp bounds it too
        prev = car["open"]
        end = prev["stamp"]
        if prev["energy"] is not None and snap.energy is not None and prev["watts"] > 0:
            added = max(0.0, snap.energy - prev["energy"])
            end = min(
                snap.stamp,
                end + added * JOULES_PER_KWH / (prev["watts"] * self.EFFICIENCY),
            )
        self._write(car, prev["stamp"], end, prev["watts"])
        car["open"] = None

    def observe(
        self,
        vin: str,
        snap: Snapshot,
        startHint: Optional[float] = None,
        ceiling: float = 0.0,
    ) -> bool:
        # ceiling: the most wall watts the car could draw here
        with self.lock:
            car = self._car(vin)
            if car["seen"] is not None and snap.stamp <= car["seen"]:
                return False
            car["seen"] = snap.stamp
            prev, last = car["open"], car["energy"]
            if snap.state == UNPLUGGED or snap.energy is not None:
                car["energy"] = None if snap.state == UNPLUGGED else snap.energy
            if snap.state != CHARGING:
                if prev is None:
                    return car["energy"] != last
                self._close(car, snap)
                return True
            if prev is None:
                self._open(car, snap, startHint, last, ceiling)
            elif snap.energy is None or prev["energy"] is None:
                # Nothing says what was drawn in between
                car["open"] = {
                    "stamp": snap.stamp,
                    "energy": snap.energy,
                    "watts": snap.watts,
                }
            elif snap.energy < prev["energy"]:
                car["open"] = None
                self._open(car, snap, None, None)
            else:
                span = snap.stamp - prev["stamp"]
                self._write(
                    car,
                    prev["stamp"],
                    snap.stamp,
                    self._wall(snap.energy - prev["energy"], span),
                )
                car["open"] = {
                    "stamp": snap.stamp,
                    "energy": snap.energy,
                    "watts": snap.watts,
                }
            return True

    def end(self, vin: str) -> bool:
        with self.lock:
            car = self.cars.get(vin)
            if not car or car["open"] is None:
                return False
            car["open"] = None
            return True

    def isOpen(self, vin: str) -> bool:
        with self.lock:
            car = self.cars.get(vin)
            return bool(car and car["open"] is not None)

    def lastStamp(self, vin: str) -> Optional[float]:
        # The open session's newest snapshot: only a later one says more
        with self.lock:
            car = self.cars.get(vin)
            return car["open"]["stamp"] if car and car["open"] else None

    def wattsBetween(
        self, start: float, end: float, live: Callable[[str], float]
    ) -> float:
        if end <= start:
            return 0.0
        with self.lock:
            cars = {
                vin: (
                    list(car["segments"]),
                    car["open"]["stamp"] if car["open"] else None,
                )
                for vin, car in self.cars.items()
            }
        now = self.clock()
        joules = 0.0
        for vin, (segments, openAt) in cars.items():
            spans = [(s, e, w) for s, e, w in segments]
            if openAt is not None:
                spans.append((openAt, now, live(vin)))
            for s, e, w in spans:
                joules += w * max(0.0, min(e, end) - max(s, start))
        return joules / (end - start)

    def prune(self, now: float) -> None:
        cutoff = now - self.KEEP_SECS
        with self.lock:
            for vin in list(self.cars):
                car = self.cars[vin]
                car["segments"] = [s for s in car["segments"] if s[1] >= cutoff]
                if (
                    not car["segments"]
                    and car["open"] is None
                    and car["energy"] is None
                ):
                    del self.cars[vin]

    def toJSON(self) -> dict:
        with self.lock:
            return copy.deepcopy(self.cars)

    def load(self, data: Optional[dict]) -> None:
        # Swaps the state in under the lock, so a ledger the watch's threads
        # already hold can be restored
        cars = {
            vin: {
                "open": dict(car["open"]) if car.get("open") else None,
                "segments": [list(s) for s in car.get("segments", [])],
                "seen": car.get("seen"),
                "energy": car.get("energy"),
            }
            for vin, car in (data or {}).items()
        }
        with self.lock:
            self.cars = cars

    @classmethod
    def fromJSON(cls, data: dict, clock=time.time) -> "ChargeLedger":
        ledger = cls(clock)
        ledger.load(data)
        return ledger
