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
