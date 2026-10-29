import copy
import threading
import time

# Tesla's published Fleet API price for streaming signals: 150,000 signals
# for $1 (developer.tesla.com pricing, checked 2026-10-28)
TELEMETRY_SIGNAL_USD = 1 / 150000
_ZERO = {"api": 0.0, "telemetry": 0.0}


def _monthOf(now: float) -> str:
    return time.strftime("%Y-%m", time.localtime(now))


def _monthStart(month: str, offset: int = 0) -> float:
    y, m = map(int, month.split("-"))
    y, m = y + (m + offset - 1) // 12, (m + offset - 1) % 12 + 1
    return time.mktime((y, m, 1, 0, 0, 0, 0, 0, -1))


def _carTotals(c) -> tuple:
    # (dollars of requests, signals) for one car's month
    return sum(t[1] for t in c["types"].values()), sum(c["fields"].values())


def _cost(api, signals) -> dict:
    return {"api": api, "telemetry": signals * TELEMETRY_SIGNAL_USD}


class FleetSpend:
    # What TWCManager itself sent to, and heard from, Tesla's paid services
    # this month: informational, it never holds anything back. Last month
    # keeps only its totals, overall and per car. The state is kept in its
    # saved form.
    def __init__(self, clock=time.time):
        self._clock = clock
        # A leaf lock: the cloud worker, the MQTT thread and the settings
        # saver all reach it
        self._lock = threading.Lock()
        self._s = self._fresh(_monthOf(clock()), dict(_ZERO), {})
        self._changed = False

    @staticmethod
    def _fresh(month, last, lastCars):
        return {
            "month": month,
            "cars": {},
            "nocar": 0.0,
            "last": last,
            "lastCars": lastCars,
        }

    def _roll(self):
        # Only forward: a clock stepping back (a box booting before it has
        # the time) must not close the month. A gap of more than a month has
        # no last month to show
        month, s = _monthOf(self._clock()), self._s
        if month <= s["month"]:
            return
        totals = {vin: _carTotals(c) for vin, c in s["cars"].items()}
        last, lastCars = dict(_ZERO), {}
        if _monthOf(_monthStart(month) - 1) == s["month"]:
            api = s["nocar"] + sum(a for a, _ in totals.values())
            last = _cost(api, sum(n for _, n in totals.values()))
            lastCars = {vin: _cost(*t) for vin, t in totals.items()}
        self._s = self._fresh(month, last, lastCars)

    def _car(self, vin):
        return self._s["cars"].setdefault(vin, {"types": {}, "fields": {}})

    def request(self, vin, kind, price) -> None:
        with self._lock:
            self._roll()
            if vin is None:
                self._s["nocar"] += price
            else:
                n, cost, _ = self._car(vin)["types"].get(kind, (0, 0.0, None))
                self._car(vin)["types"][kind] = [n + 1, cost + price, self._clock()]
            self._changed = True

    def signal(self, vin, field) -> None:
        with self._lock:
            self._roll()
            fields = self._car(vin)["fields"]
            fields[field] = fields.get(field, 0) + 1
            self._changed = True

    def month(self) -> dict:
        now = self._clock()
        with self._lock:
            self._roll()
            s = self._s
            totals = [_carTotals(c) for c in s["cars"].values()]
            api = s["nocar"] + sum(a for a, _ in totals)
            signals = sum(n for _, n in totals)
            last, month = dict(s["last"]), s["month"]
        out = dict(_cost(api, signals), signals=signals, last=last, pace=None)
        start, end = _monthStart(month), _monthStart(month, 1)
        # A first day's spend says too little about the rest of the month
        if now - start >= 86400:
            scale = (end - start) / (now - start)
            out["pace"] = {
                "api": api * scale,
                "telemetry": out["telemetry"] * scale,
                "by": time.strftime("%b 1", time.localtime(end)),
            }
        return out

    def car(self, vin) -> dict:
        with self._lock:
            self._roll()
            c = copy.deepcopy(self._s["cars"].get(vin, {"types": {}, "fields": {}}))
            last = dict(self._s["lastCars"].get(vin, _ZERO))
        api, signals = _carTotals(c)
        types = [
            {"kind": k, "count": n, "cost": cost, "last": at}
            for k, (n, cost, at) in c["types"].items()
        ]
        fields = [{"field": f, "count": n} for f, n in c["fields"].items()]
        return dict(
            _cost(api, signals),
            requests=sum(t["count"] for t in types),
            types=sorted(types, key=lambda t: -t["cost"]),
            signals=signals,
            fields=sorted(fields, key=lambda f: -f["count"]),
            last=last,
        )

    def changed(self) -> bool:
        with self._lock:
            changed, self._changed = self._changed, False
            return changed

    def toJSON(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._s)

    def load(self, data) -> None:
        # Anything malformed starts the month empty
        try:
            s = copy.deepcopy(data)
            _monthStart(s["month"])
            for c in s["cars"].values():
                for n, cost, at in c["types"].values():
                    int(n), float(cost), float(at)
                _carTotals(c)
            for t in (s["last"], *s.setdefault("lastCars", {}).values()):
                float(t["api"]) + float(t["telemetry"])
            float(s["nocar"])
        except (TypeError, KeyError, ValueError, AttributeError):
            return
        with self._lock:
            self._s = s
            self._roll()
