import threading
import time
from typing import Optional

# Tesla's published Fleet API price for streaming signals: 150,000 signals
# for $1 (developer.tesla.com pricing, checked 2026-10-28)
TELEMETRY_SIGNAL_USD = 1 / 150000


def _monthOf(now: float) -> str:
    return time.strftime("%Y-%m", time.localtime(now))


def _previous(month: str) -> str:
    y, m = map(int, month.split("-"))
    return "%04d-%02d" % ((y, m - 1) if m > 1 else (y - 1, 12))


def _monthStart(month: str, offset: int = 0) -> float:
    y, m = map(int, month.split("-"))
    m += offset
    y, m = y + (m - 1) // 12, (m - 1) % 12 + 1
    return time.mktime((y, m, 1, 0, 0, 0, 0, 0, -1))


class FleetSpend:
    # What TWCManager itself sent to, and heard from, Tesla's paid services
    # this month: informational, it never holds anything back. Last month
    # keeps only its totals, overall and per car.
    def __init__(self, clock=time.time):
        self._clock = clock
        # A leaf lock: the cloud worker, the MQTT thread and the settings
        # saver all reach it
        self._lock = threading.Lock()
        self._reset(_monthOf(clock()), {"api": 0.0, "telemetry": 0.0}, {})
        self._changed = False

    def _reset(self, month, last, lastCars):
        self._month, self._last, self._lastCars = month, last, lastCars
        self._cars = {}
        self._nocar = 0.0

    def _totals(self):
        api = self._nocar + sum(
            t[1] for c in self._cars.values() for t in c["types"].values()
        )
        signals = sum(n for c in self._cars.values() for n in c["fields"].values())
        return api, signals

    def _roll(self, now):
        # A new month, whether seen live or after a restart across the
        # boundary, closes the old one; a gap of more than a month has no
        # last month to show
        month = _monthOf(now)
        if month != self._month:
            api, signals = self._totals()
            last = {"api": api, "telemetry": signals * TELEMETRY_SIGNAL_USD}
            lastCars = {
                vin: {
                    "api": sum(t[1] for t in c["types"].values()),
                    "telemetry": sum(c["fields"].values()) * TELEMETRY_SIGNAL_USD,
                }
                for vin, c in self._cars.items()
            }
            if _previous(month) != self._month:
                last, lastCars = {"api": 0.0, "telemetry": 0.0}, {}
            self._reset(month, last, lastCars)

    def _car(self, vin):
        return self._cars.setdefault(vin, {"types": {}, "fields": {}})

    def request(self, vin, kind, price, now=None) -> None:
        now = self._clock() if now is None else now
        with self._lock:
            self._roll(now)
            if vin is None:
                self._nocar += price
            else:
                count, cost, _ = self._car(vin)["types"].get(kind, (0, 0.0, None))
                self._car(vin)["types"][kind] = (count + 1, cost + price, now)
            self._changed = True

    def signal(self, vin, field, now=None) -> None:
        now = self._clock() if now is None else now
        with self._lock:
            self._roll(now)
            fields = self._car(vin)["fields"]
            fields[field] = fields.get(field, 0) + 1
            self._changed = True

    def month(self, now=None) -> dict:
        now = self._clock() if now is None else now
        with self._lock:
            self._roll(now)
            api, signals = self._totals()
            last, month = dict(self._last), self._month
        telemetry = signals * TELEMETRY_SIGNAL_USD
        start, end = _monthStart(month), _monthStart(month, 1)
        # A first day's spend says too little about the rest of the month
        pace = None
        if now - start >= 86400:
            scale = (end - start) / (now - start)
            pace = {
                "api": api * scale,
                "telemetry": telemetry * scale,
                "by": time.strftime("%b 1", time.localtime(end)),
            }
        return {
            "api": api,
            "signals": signals,
            "telemetry": telemetry,
            "pace": pace,
            "last": last,
        }

    def car(self, vin, now=None) -> dict:
        now = self._clock() if now is None else now
        with self._lock:
            self._roll(now)
            c = self._cars.get(vin, {"types": {}, "fields": {}})
            types = [
                {"kind": k, "count": n, "cost": cost, "last": last}
                for k, (n, cost, last) in c["types"].items()
            ]
            fields = [{"field": f, "count": n} for f, n in c["fields"].items()]
            last = dict(self._lastCars.get(vin, {"api": 0.0, "telemetry": 0.0}))
        types.sort(key=lambda t: -t["cost"])
        fields.sort(key=lambda f: -f["count"])
        signals = sum(f["count"] for f in fields)
        return {
            "api": sum(t["cost"] for t in types),
            "requests": sum(t["count"] for t in types),
            "types": types,
            "signals": signals,
            "telemetry": signals * TELEMETRY_SIGNAL_USD,
            "fields": fields,
            "last": last,
        }

    def changed(self) -> bool:
        with self._lock:
            changed, self._changed = self._changed, False
            return changed

    def toJSON(self) -> dict:
        with self._lock:
            return {
                "month": self._month,
                "cars": {
                    vin: {
                        "types": {k: list(v) for k, v in c["types"].items()},
                        "fields": dict(c["fields"]),
                    }
                    for vin, c in self._cars.items()
                },
                "nocar": self._nocar,
                "last": dict(self._last),
                "lastCars": {vin: dict(t) for vin, t in self._lastCars.items()},
            }

    def load(self, data) -> None:
        # Anything malformed starts the month empty
        with self._lock:
            try:
                month = data["month"]
                _previous(month)
                cars = {
                    str(vin): {
                        "types": {
                            str(k): (int(n), float(cost), last)
                            for k, (n, cost, last) in c["types"].items()
                        },
                        "fields": {str(f): int(n) for f, n in c["fields"].items()},
                    }
                    for vin, c in data["cars"].items()
                }
                nocar = float(data.get("nocar", 0.0))
                last = {k: float(data["last"][k]) for k in ("api", "telemetry")}
                lastCars = {
                    str(vin): {k: float(t[k]) for k in ("api", "telemetry")}
                    for vin, t in data.get("lastCars", {}).items()
                }
            except (TypeError, KeyError, ValueError, AttributeError):
                return
            self._month, self._cars, self._nocar = month, cars, nocar
            self._last, self._lastCars = last, lastCars
            self._roll(self._clock())
