# Where each car is, decided fresh from events marked as observations
# arrive: HOME, NOT_HOME or PRESUMED (no evidence either way; ties land here)
import functools
import logging
from typing import Any, List, Optional, Tuple

from .Providers.Capability import SET_CHARGE_LIMIT
from .Registry.Fields import TELEMETRY_SOURCES

logger = logging.getLogger(__name__.split(".")[0] + ".Vehicle.Presence")

HOME, PRESUMED, NOT_HOME = "home", "presumed", "notHome"
# applicable's verdicts on one message
SEND, NOT_HERE, HOLD, HOME_NOW = "send", "not here", "hold", "home"
HOME_RADIUS_FT = 1000
HOME_FIX_MAX_AGE = 600.0
REACH_WINDOW = 300.0
# A fast charger type other than these is not a home charger
HOME_CHARGER_TYPES = frozenset({"ACSingleWireCAN", "MCSingleWireCAN", "<invalid>", ""})
_PLUGGED = frozenset({"Charging", "Starting", "Stopped", "Complete", "NoPower"})
# A plug report from these needs no position: the charger or the radio is here
_STRONG = frozenset({"TWC", "Gen2TWC", "Gen3TWC", "TeslaBLE"})
_MOVING = frozenset({"D", "R", "N"})
# Feet per degree of latitude; the box test uses it for longitude too
_FT_PER_DEGREE = 364488.888
NEVER = float("-inf")


@functools.lru_cache(maxsize=None)
def _warnNoHome():
    logger.warning(
        "No home location set: cars reached only through vehicle APIs are "
        "not treated as home (set it on the Settings page)"
    )


def _within(master, lat, lon) -> Optional[bool]:
    home = master.getHomeLatLon()
    if home[0] == 10000:
        _warnNoHome()
        return None
    config = (getattr(master, "config", None) or {}).get("config", {})
    deg = float(config.get("atHomeRadius", HOME_RADIUS_FT)) / _FT_PER_DEGREE
    return abs(home[0] - lat) <= deg and abs(home[1] - lon) <= deg


def _fix(master, v) -> Tuple[Optional[bool], float]:
    # The newest position: inside the radius (None if unknown) and its time
    lat, lon = v.latest("lat"), v.latest("lon")
    if lat is None or lon is None:
        return None, NEVER
    return _within(master, lat.value, lon.value), max(lat.at, lon.at)


def inside(master, v) -> Optional[bool]:
    return _fix(master, v)[0]


def _plug(field, obs) -> Optional[bool]:
    # True plugged, False unplugged, None says nothing
    if field == "pluggedIn":
        return obs.value if isinstance(obs.value, bool) else None
    if obs.source == "HomeAssistant":
        # Home Assistant reports "Stopped" for an unplugged car too
        return True if obs.value == "Charging" else None
    if obs.value == "Disconnected":
        return False
    return True if obs.value in _PLUGGED else None


def _awayCharger(v, obs) -> bool:
    # A fast charger, no cable, or more power than the car's own AC maximum
    # at its voltage: plugged in, but not to a home charger
    def since(field):
        o = v.reported(field).get(obs.source)
        return o.value if o is not None and o.at >= obs.at else None

    kind, amps = since("fastChargerType"), since("chargeCurrentRequestMax")
    power, volts = since("chargerPower") or 0, max(v.value("voltage") or 0, 240)
    return (
        since("fastChargerPresent") is True
        or kind not in HOME_CHARGER_TYPES | {None}
        or since("connChargeCable") in ("<invalid>", "")
        or (bool(amps) and power * 1000 > amps * volts * 1.1)
    )


def _plugEvidence(v, field, obs, here) -> Optional[bool]:
    value = _plug(field, obs)
    car = here is True and not (value and _awayCharger(v, obs))
    return value if obs.source in _STRONG or car else None


def _driving(field, value) -> bool:
    return value == "driving" if field == "state" else value in _MOVING


def noteObservation(master, vehicle, field, obs, previous) -> None:
    # The registry's onObserve hook, for every field: obs is the incoming
    # reading, previous the same source's reading before it
    at, before = obs.at, None if previous is None else previous.value
    if field == "bleReach":
        if isinstance(obs.value, bool):
            vehicle.mark("contact" if obs.value else "absent", at)
    elif field in ("lat", "lon"):
        # With the same source's other coordinate: one may change alone
        other = vehicle.reported("lon" if field == "lat" else "lat").get(obs.source)
        lat, lon = (obs, other) if field == "lat" else (other, obs)
        if other is not None and _within(master, lat.value, lon.value) is False:
            vehicle.mark("outside", at)
        # A BLE location read answers only within range
        if obs.source == "TeslaBLE" or twcVinAt(master, vehicle) > NEVER:
            learnHome(master, vehicle, lat, lon)
    elif field in ("shiftState", "state"):
        here = inside(master, vehicle)
        moving, wasMoving = _driving(field, obs.value), _driving(field, before)
        if moving and not wasMoving and here is not False:
            vehicle.mark("driveStart", at)
        elif (
            wasMoving and not moving and here and (field == "state" or obs.value == "P")
        ):
            vehicle.mark("driveEnd", at)
    elif field in ("pluggedIn", "chargingState"):
        plugged = _plugEvidence(vehicle, field, obs, inside(master, vehicle))
        was = None if previous is None else _plug(field, previous)
        if plugged is False and (was is True or obs.source == "Gen2TWC"):
            # A car parked unplugged reports Disconnected again and again
            vehicle.mark("unplug", at)
        elif plugged is True and was is not True:
            vehicle.mark("plugIn", at)
            # A pushed or Gen3 plug-in; a Gen2 names its car (recordVehicleVIN)
            if obs.source in TELEMETRY_SOURCES | {"Gen3TWC"}:
                from .Reconcile.Targets import onArrival

                onArrival(master, vehicle)


def pluggedNow(master, v) -> bool:
    if twcVinAt(master, v) > NEVER:
        # A charging point names the car and reports a plug
        return True
    here, best = inside(master, v), None
    for field in ("pluggedIn", "chargingState"):
        for obs in v.reported(field).values():
            value = _plugEvidence(v, field, obs, here)
            key = (obs.at, obs.source in _STRONG)
            if value is not None and (best is None or key > best[0]):
                best = (key, value)
    return best is not None and best[1]


def telemetrySaysPlugged(master, v) -> bool:
    # A live telemetry feed says the cable is in
    return v.syncSource in TELEMETRY_SOURCES and v.value("pluggedIn") is True


def drivingNow(v) -> bool:
    state, shift = v.latest("state"), v.latest("shiftState")
    if shift is not None and (state is None or shift.at >= state.at):
        return shift.value in _MOVING
    return state is not None and state.value == "driving"


def _pointPlugged(master, pointId) -> bool:
    # A Gen2 keeps naming its car at 00; a Gen3 clears twcVin on unplug
    for slave in master.getSlaveTWCs():
        if getattr(slave, "twcid", None) == pointId:
            return slave.reportedState != 0x00
    return pointId.startswith("Gen3TWC-")


def twcVinAt(master, v) -> float:
    # A point naming the car is home evidence while it still reports a plug
    # and the car has shown no movement since
    obs = v.latest("twcVin")
    if obs is None or not obs.value or not _pointPlugged(master, obs.value):
        return NEVER
    if max(v.markAt("driveStart"), v.markAt("outside")) > obs.at:
        return NEVER
    return obs.at


def decide(master, v) -> Tuple[str, str, float]:
    # (presence, the deciding event, its time)
    if v.isAnonymous:
        return HOME, "on a charging point", NEVER
    here, fixAt = _fix(master, v)
    # Parked: in P, or with no gear known, not driving
    shift = v.value("shiftState")
    parked = shift == "P" if shift is not None else not drivingNow(v)
    home = max(
        (twcVinAt(master, v), "named by its charging point"),
        (v.markAt("plugIn"), "plugged in"),
        (v.markAt("contact"), "BLE contact"),
        (fixAt if here and parked else NEVER, "parked inside the radius"),
    )
    if here is False and fixAt > home[0]:
        return NOT_HOME, "a fix outside the radius", fixAt
    if drivingNow(v):
        return PRESUMED, "driving", NEVER
    if home[0] > max(v.markAt("driveStart"), v.markAt("absent"), v.markAt("unplug")):
        return HOME, home[1], home[0]
    if v.markAt("unplug") > home[0]:
        return NOT_HOME, "unplug", v.markAt("unplug")
    return PRESUMED, "no newer evidence", NEVER


def presence(master, v) -> str:
    return decide(master, v)[0]


def homeForLimits(master, v) -> bool:
    return presence(master, v) != NOT_HOME


def pluggedHere(master, v) -> bool:
    return presence(master, v) == HOME and pluggedNow(master, v)


def leaving(v) -> float:
    return max(v.markAt("unplug"), v.markAt("driveStart"))


def arriving(v) -> float:
    return max(v.markAt("outside"), v.markAt("driveEnd"))


def departed(master, v) -> Optional[float]:
    # When the car was seen gone, or leaving while unplugged here (its last
    # departure newer than its last arrival); None while it stays
    state, _, at = decide(master, v)
    if state == HOME and not pluggedNow(master, v):
        return leaving(v) if leaving(v) > arriving(v) else None
    return at if state == NOT_HOME else None


def applicable(master, v, capability, restore) -> Tuple[str, str]:
    # Whether a message, a stop aside, still fits where the car is, and the
    # cell that says so. Missing event times are NEVER; a tie holds
    state, plugged = presence(master, v), pluggedNow(master, v)
    gone = departed(master, v)
    if restore:
        if state == HOME:
            return (SEND, "leaving") if gone is not None else (HOME_NOW, "home")
        return (HOLD, "not known here") if state == PRESUMED else (SEND, "away")
    if state == NOT_HOME:
        return NOT_HERE, "away"
    if capability != SET_CHARGE_LIMIT:
        # A start or a rate needs the plug; a car we hold a limit on and
        # saw no unplug from is presumed still on it
        presumed = state == PRESUMED and v.markAt("unplug") == NEVER
        if plugged or presumed and v.vin in master.heldLimits.holding():
            return SEND, "plugged in"
        return (NOT_HERE if state == HOME else HOLD), "not plugged in"
    if state == HOME:
        if gone is not None:
            return NOT_HERE, "leaving"
        tie = not plugged and leaving(v) == arriving(v) > NEVER
        return (HOLD, "tie") if tie else (SEND, "home")
    if drivingNow(v):
        return HOLD, "driving"
    # Not known to be here: a plug says so
    return (SEND, "plugged in") if plugged else (HOLD, "not known here")


def candidates(master, capability=None) -> List:
    # Cars an unnamed charging point may serve. A broadcast (capability)
    # reaches cars on the Tesla API's list or with a BLE key that may be
    # home, and Home Assistant cars at home that a TWC reports by VIN
    registry, order = master.vehicleRegistry, master.providerOrder
    out = []
    for v in registry.vehicles():
        if v.isAnonymous or twcVinAt(master, v) > NEVER:
            continue
        state = presence(master, v)
        if capability is None:
            reach = max(v.markAt("absent"), registry.now() - REACH_WINDOW)
            near = reach < v.markAt("contact")
            ok = state != NOT_HOME or inside(master, v) or near
        else:
            api = v.attachment("TeslaAPI") is not None or "TeslaBLE" in v.claims
            ha = v.attachment("HomeAssistant") is not None and state == HOME
            ok = bool(order.candidates(v, capability)) and (
                (state != NOT_HOME and api) or bool(ha and master.getTWCbyVIN(v.vin))
            )
        if ok:
            out.append(v)
    return out


def homeCars(master, attachment) -> List[Tuple[Any, Any]]:
    pairs = master.vehicleRegistry.attached(attachment)
    return [(v, a) for v, a in pairs if homeForLimits(master, v)]


def learnHome(master, vehicle, lat, lon) -> bool:
    # A car a TWC names, or BLE reads, is here: a fresh fix outside the
    # radius, or with no home yet, moves home to it. A stale fix may be
    # wherever the car last had reception: the GPS time its source gave
    # (observed before the position) dates it, else its reading's time. A
    # fix not read over BLE must be read after the car reached its TWC (the
    # later of its plug-in and the VIN read; a Gen2's plug-in mark stays on
    # the anonymous session the VIN read replaces), else it may predate the
    # drive home
    if lat is None or lon is None:
        return False
    named = getattr(vehicle.latest("twcVin"), "at", NEVER)
    if lat.source != "TeslaBLE" and lat.at < max(vehicle.markAt("plugIn"), named):
        return False
    gps = vehicle.reported("gpsAsOf").get(lat.source)
    fixAt = lat.at if gps is None else min(lat.at, gps.value)
    if master.vehicleRegistry.now() - fixAt > HOME_FIX_MAX_AGE:
        return False
    if _within(master, lat.value, lon.value):
        return False
    logger.info("Home set to %s's position on a TWC", vehicle.key)
    master.setHomeLat(lat.value)
    master.setHomeLon(lon.value)
    master.queue_background_task({"cmd": "sunrise"})
    master.queue_background_task({"cmd": "saveSettings"})
    return True
