# Forecast helpers shared by Pricing modules that offer the Forecasting
# capability. A forecast is a list of slots in time order, each
# {"timestamp": aware datetime, "importPrice": float, ...}.
from datetime import datetime, timedelta


def cheapestWindow(
    forecast, numHours, startHour=None, endHour=None, slotMinutes=30, tz=None
):
    """The cheapest run of numHours of contiguous slots, optionally starting
    between startHour and endHour (local hours in tz; None is the host's
    zone). Returns {startHour, startTimestamp, avgPrice, totalCost} or None.
    Ties keep the earliest."""
    if not forecast or numHours <= 0 or numHours > 24:
        return None

    slot = timedelta(minutes=slotMinutes)
    size = int(numHours * 60 // slotMinutes)
    best = None
    for i in range(len(forecast) - size + 1):
        window = forecast[i : i + size]
        hour = window[0]["timestamp"].astimezone(tz).hour

        if startHour is not None:
            if endHour is None:
                allowed = hour >= startHour
            elif startHour <= endHour:
                allowed = startHour <= hour < endHour
            else:
                allowed = hour >= startHour or hour < endHour
            if not allowed:
                continue

        # A missing slot breaks contiguity. Compared as instants: subtracting
        # two times in one zone ignores a DST change between them.
        if any(
            b["timestamp"].timestamp() - a["timestamp"].timestamp()
            != slot.total_seconds()
            for a, b in zip(window, window[1:])
        ):
            continue

        avg = sum(e["importPrice"] for e in window) / size
        if best is None or avg < best["avgPrice"]:
            best = {
                "startHour": hour,
                "startTimestamp": window[0]["timestamp"].isoformat(),
                "avgPrice": avg,
                "totalCost": avg * numHours,
            }
    return best


def priceRange(forecast):
    """(lowest, highest) import price, or None for an empty forecast."""
    if not forecast:
        return None
    prices = [entry["importPrice"] for entry in forecast]
    return min(prices), max(prices)


def windowActive(startTimestamp, hours, now):
    """1 while now falls in the window starting at startTimestamp (ISO 8601)."""
    if not startTimestamp:
        return 0
    delta = (now - datetime.fromisoformat(startTimestamp)).total_seconds()
    return 1 if 0 <= delta < hours * 3600 else 0


def serialise(forecast, hoursAhead, slotMinutes=30):
    """The first hoursAhead of the forecast, JSON-ready."""
    out = []
    for entry in forecast[: int(hoursAhead * 60 // slotMinutes)]:
        entry = dict(entry)
        entry["timestamp"] = entry["timestamp"].isoformat()
        out.append(entry)
    return out


def latchedWindow(previousStart, hours, now, search):
    """The cheapest window's start: the one already under way, if now is in
    it, else search()'s. A forecast only looks ahead, so once a window has
    begun a fresh search would find a later one and cut it short."""
    if windowActive(previousStart, hours, now):
        return previousStart
    best = search()
    return best["startTimestamp"] if best else None


class ForecastingPricing:
    """Base for Pricing modules with the Forecasting capability. It reads the
    module's config section and its cheapWindow, and gives policy the
    forecast attributes. A subclass supplies its prices, updateForecast() to
    refresh its forecast, and _forecast(), which returns (slots, now, tz)
    without blocking: policy reads these attributes on the main loop."""

    capabilities = {"AdvancePricing": True, "Forecasting": True}
    status = False

    def __init__(self, master):
        self.master = master
        name = type(self).__name__
        self.configPricing = (
            master.config.get("pricing", {}).get(name.replace("Pricing", ""), {})
        )
        window = self.configPricing.get("cheapWindow", {})
        self.cheapWindowHours = window.get("hours", 0)
        self.cheapWindowStartHour = window.get("startHour")
        self.cheapWindowEndHour = window.get("endHour")
        self._cheapStart = None
        self.status = self.configPricing.get("enabled", False)
        if not self.status:
            master.releaseModule("lib.TWCManager.Pricing", name)

    def getCapabilities(self, capability):
        return self.capabilities.get(capability, False)

    @staticmethod
    def _known(price):
        # Absent, so policy's modules.* lookup falls back to an unresolved
        # reference instead of a number
        if price is None:
            raise AttributeError("price unknown")
        return price

    def getPriceForecast(self, hoursAhead=24):
        self.updateForecast()
        return serialise(self._forecast()[0], hoursAhead)

    def getCheapestWindow(self, numHours, startHour=None, endHour=None):
        self.updateForecast()
        forecast, _, tz = self._forecast()
        return cheapestWindow(forecast, numHours, startHour, endHour, tz=tz)

    @property
    def forecastMinPrice(self):
        return self._known((priceRange(self._forecast()[0]) or (None,))[0])

    @property
    def forecastMaxPrice(self):
        return self._known((priceRange(self._forecast()[0]) or (None, None))[1])

    @property
    def cheapWindowStart(self):
        if not self.cheapWindowHours or self.cheapWindowHours <= 0:
            return None
        forecast, now, tz = self._forecast()
        self._cheapStart = latchedWindow(
            self._cheapStart,
            self.cheapWindowHours,
            now,
            lambda: cheapestWindow(
                forecast,
                self.cheapWindowHours,
                self.cheapWindowStartHour,
                self.cheapWindowEndHour,
                tz=tz,
            ),
        )
        return self._cheapStart

    @property
    def cheapWindowActive(self):
        start = self.cheapWindowStart
        return windowActive(start, self.cheapWindowHours, self._forecast()[1])
