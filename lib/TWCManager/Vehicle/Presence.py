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
SEND, NOT_HERE, HOLD = "send", "not here", "hold"
HOME_RADIUS_FT = 1000
HOME_FIX_MAX_AGE = 600.0
REACH_WINDOW = 300.0
# The least time between status checks of a car known asleep that no live
# feed reports on: at home, and anywhere else
HOME_ASLEEP_SECS, AWAY_ASLEEP_SECS = 900.0, 1800.0
PILOT_MATCH_TOLERANCE, PLUGGED_JUST_NOW = 1, 120.0
_BLE_CHARGE = ("chargingState", "availableCurrent", "actualCurrent")
# A fast charger type other than these is not a home charger
HOME_CHARGER_TYPES = frozenset({"ACSingleWireCAN", "MCSingleWireCAN", "<invalid>", ""})
_PLUGGED = frozenset({"Charging", "Starting", "Stopped", "Complete", "NoPower"})
# A plug report from these needs no position: the charger or the radio is here
_STRONG = frozenset({"TWC", "Gen2TWC", "Gen3TWC", "TeslaBLE"})
_CHARGERS = frozenset({"Gen2TWC", "Gen3TWC"})
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
    from .Reconcile import Targets

    if field == "bleReach":
        if isinstance(obs.value, bool):
            back = obs.value and not _reached(master, vehicle)
            vehicle.mark("contact" if obs.value else "absent", at)
            # A car heard again may be the one on an unnamed plug
            if back and not vehicle.value("twcVin"):
                master.attributePlugs()
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
        if obs.seen and moving != wasMoving:
            vehicle.mark("lifeAt", at)
        if moving and not wasMoving and here is not False:
            vehicle.mark("driveStart", at)
            # The drive start plans the restore while BLE is still in range
            Targets._restoreDeparted(master)
        elif (
            wasMoving and not moving and here and (field == "state" or obs.value == "P")
        ):
            vehicle.mark("driveEnd", at)
    elif field in ("pluggedIn", "chargingState"):
        plugged = _plugEvidence(vehicle, field, obs, inside(master, vehicle))
        # A feed's retained copy, redelivered on a reconnect, is no plug event
        plugged = None if obs.source in TELEMETRY_SOURCES and not obs.seen else plugged
        was = None if previous is None else _plug(field, previous)
        # No previous reading counts as seen. A retained copy since the feed's
        # last report: the plug events say what that report was, if any is
        # known (none after a restart: then the copy is the best word)
        if obs.source in TELEMETRY_SOURCES and not getattr(previous, "seen", True):
            plugIn, unplug = vehicle.markAt("plugIn"), vehicle.markAt("unplug")
            was = was if plugIn == unplug else plugIn > unplug
        if plugged is False and (was is True or obs.source == "Gen2TWC"):
            # A car parked unplugged reports Disconnected again and again
            vehicle.mark("unplug", at)
            if obs.seen:
                vehicle.mark("lifeAt", at)
        elif plugged is True and was is not True:
            vehicle.mark("plugIn", at)
            # Not a retained copy, nor a charger's first reading of a plug
            # it did not see happen (a restart)
            if obs.seen and (obs.source not in _CHARGERS or was is False):
                vehicle.mark("lifeAt", at)
            # A pushed or attributed plug-in; a Gen2's VIN arrives on its own
            if obs.source in TELEMETRY_SOURCES | {"Gen2TWC", "Gen3TWC"}:
                Targets.onArrival(master, vehicle)
            if obs.source not in _CHARGERS and not vehicle.value("twcVin"):
                master.attributePlugs()


def pluggedNow(master, v, unknown=False, since=NEVER) -> Optional[bool]:
    # The newest plug reading after since; unknown when nothing says
    here, best = inside(master, v), None
    for field in ("pluggedIn", "chargingState"):
        for obs in v.reported(field).values():
            value = _plugEvidence(v, field, obs, here)
            # A Gen2's plug counts only while it names the car, below
            value = None if value and obs.source == "Gen2TWC" else value
            key = (obs.at, obs.source in _STRONG)
            if value is not None and obs.at > since and (best is None or key > best[0]):
                best = (key, value, obs.source)
    # A charging point naming the car says plugged unless another source
    # read it unplugged since; a Gen2 re-plugged keeps its old naming
    named = twcVinAt(master, v)
    if named > NEVER and (
        not best or best[1] or best[2] == "Gen2TWC" or best[0][0] <= named
    ):
        return True
    return unknown if best is None else best[1]


def telemetryPlug(v, since=NEVER, seen=False) -> Optional[bool]:
    # The live feed's own plug reading, not older than since (seen: nor retained)
    obs = v.reported("pluggedIn").get(v.syncSource)
    live = obs is not None and obs.at >= since and v.syncSource in TELEMETRY_SOURCES
    return obs.value if live and (obs.seen or not seen) else None


def unpluggedAt(v) -> float:
    # When the newest reading saying unplugged was made
    fields = ("pluggedIn", "chargingState")
    obs = [o for f in fields for o in v.reported(f).values() if _plug(f, o) is False]
    return max((o.at for o in obs), default=NEVER)


def checkPlug(master, v, since) -> Optional[bool]:
    # Plugged in, after a charging point's status edge at since? Never wakes:
    # a live feed reports a change in seconds (a retained copy is no report);
    # an unplug wakes a sleeping car; else the read cascade, where a cloud API
    # reads only a car its free online check finds awake, past the stored
    # reading and online answer that an unplug just made stale
    if (feed := telemetryPlug(v, since, seen=True)) is not None:
        return feed
    ble, now = master.providerOrder.provider("TeslaBLE"), master.vehicleRegistry.now()
    # Asleep, or not seen awake lately (the range scan alone answered)
    if "TeslaBLE" in v.claims and ble.module.ping(v.vin):
        if asleep(v, now - REACH_WINDOW) is not False:
            return True
    car = v.attachment("TeslaAPI")
    if car is not None:
        car.statusDeferral, car.staleBefore = 0, since
    v.read("chargingState", 0, force=True)
    return pluggedNow(master, v, None, since)


def drivingNow(v) -> bool:
    # The feed's own state: a provider's sleep check never masks a drive. A
    # car plugged in, named by a TWC or drawing current since that reading
    # has stopped, whether or not the drive's end was reported
    state, shift = v.telemetryState()[0], v.latest("shiftState")
    if shift is not None and (state is None or shift.at >= state.at):
        driving = shift if shift.value in _MOVING else None
    else:
        driving = state if state is not None and state.value == "driving" else None
    named, charging = v.latest("twcVin"), v.latest("chargingState")
    amps = v.latest("actualCurrent")
    stopped = [v.markAt("plugIn")]
    stopped += [named.at] if named is not None and named.value else []
    stopped += [charging.at] if charging and charging.value == "Charging" else []
    stopped += [amps.at] if amps is not None and (amps.value or 0) >= 1 else []
    return driving is not None and driving.at >= max(stopped)


def covered(master, v) -> bool:
    # A live report-on-change feed tells of the car's changes itself
    return isinstance(master.vehicleRegistry.sourceLiveAt(v.syncSource, v), float)


def asleep(v, lately=NEVER) -> Optional[bool]:
    # True: the newest asleep reading outranks every sign of life (a seen state
    # other than asleep or offline, a seen drive or plug change, current drawn).
    # False: a sign of life newer than it and than lately. Else unknown
    states, amps = v.reported("state").values(), v.latest("actualCurrent")
    life = [o.at for o in states if o.seen and o.value not in ("asleep", "offline")]
    life += [v.markAt("lifeAt")]
    life = max(life + ([amps.at] if amps and (amps.value or 0) >= 1 else []))
    slept = max([o.at for o in states if o.value == "asleep"], default=NEVER)
    if slept >= life and slept > NEVER:
        return True
    return False if life > max(slept, lately) else None


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


def pluggedHere(master, v) -> bool:
    return presence(master, v) == HOME and pluggedNow(master, v)


def leaving(v) -> float:
    return max(v.markAt("unplug"), v.markAt("driveStart"))


def arriving(v) -> float:
    return max(v.markAt("outside"), v.markAt("driveEnd"))


def returned(v) -> float:
    # When the car last came back: parked inside the radius, or plugged in
    return max(v.markAt("driveEnd"), v.markAt("plugIn"))


def departed(master, v) -> Optional[float]:
    # When the car was seen gone, or leaving (unplugged here, or driving off
    # after it came back) since it last arrived; None while it stays
    state, _, at = decide(master, v)
    if state == NOT_HOME:
        return at
    away = state == HOME or drivingNow(v) and returned(v) > NEVER
    away = away and leaving(v) > arriving(v) and not pluggedNow(master, v)
    return leaving(v) if away else None


def applicable(master, v, capability) -> Tuple[str, str]:
    # Whether a message, a stop or a restore aside (those always go), still
    # fits where the car is, and the cell that says so. Missing event times
    # are NEVER; a tie holds
    state, plugged = presence(master, v), pluggedNow(master, v, None)
    if state == NOT_HOME:
        return NOT_HERE, "away"
    if capability != SET_CHARGE_LIMIT:
        # A start or a rate needs the plug. A naming outlasts a Gen2 00 until
        # the car leaves or reads unplugged; a held car never unplugged stays
        named = v.latest("twcVin")
        at = named.at if named is not None and named.value else NEVER
        named = at > leaving(v) and pluggedNow(master, v, True, since=at)
        presumed = state == PRESUMED or plugged is None
        presumed = presumed and v.markAt("unplug") == NEVER
        if plugged or named or presumed and v.vin in master.heldLimits.holding():
            return SEND, "plugged in"
        return (NOT_HERE if state == HOME else HOLD), "not plugged in"
    if state == HOME:
        if departed(master, v) is not None:
            return NOT_HERE, "leaving"
        tie = not plugged and leaving(v) == arriving(v) > NEVER
        return (HOLD, "tie") if tie else (SEND, "home")
    if drivingNow(v):
        return HOLD, "driving"
    # Not known to be here: a plug says so
    moving = inside(master, v) and v.value("shiftState") not in (None, "P")
    why = "not parked" if moving else "not known here"
    return (SEND, "plugged in") if plugged else (HOLD, why)


def candidates(master, capability=None) -> List:
    # Cars an unnamed charging point may serve, none named elsewhere. A
    # broadcast (capability) reaches listed or BLE-keyed cars that may be
    # home, and Home Assistant cars at home that a TWC reports by VIN
    registry, order = master.vehicleRegistry, master.providerOrder
    out = []
    for v in registry.vehicles():
        if v.isAnonymous or twcVinAt(master, v) > NEVER:
            continue
        state = presence(master, v)
        if capability is None:
            near = inside(master, v) or _reached(master, v)
            ok = (state != NOT_HOME or near) and not v.value("twcVin")
        else:
            api = v.attachment("TeslaAPI") is not None or "TeslaBLE" in v.claims
            ha = v.attachment("HomeAssistant") is not None and state == HOME
            ok = bool(order.candidates(v, capability)) and (
                (state != NOT_HOME and api) or bool(ha and master.getTWCbyVIN(v.vin))
            )
        if ok:
            out.append(v)
    return out


def _reached(master, v) -> bool:
    reach = max(v.markAt("absent"), master.vehicleRegistry.now() - REACH_WINDOW)
    return reach < v.markAt("contact")


def pick(master, offered=None, actual=0, read=False, since=NEVER):
    # A VIN-less plug's car (plugged in at since): plugged in just now, else
    # BLE-read at this pilot and draw (given an offer, or a function giving
    # the offer now), else reached or inside
    now, pool = master.vehicleRegistry.now(), candidates(master)
    offer = offered if callable(offered) else lambda: offered
    offered = offer()

    def fresh(v, field, source="TeslaBLE", window=REACH_WINDOW):
        o = v.reported(field).get(source) if source else v.latest(field)
        return o if o is not None and now - o.at <= window else None

    just = [(v, fresh(v, "pluggedIn", None, PLUGGED_JUST_NOW)) for v in pool]
    just = [v for v, o in just if o and _plugEvidence(v, "pluggedIn", o, True)]
    just = [v for v in just if presence(master, v) != NOT_HOME]
    if len(just) == 1:
        return just[0]

    def fits(v):
        # BLE says: False not plugged in here, True at this pilot and draw;
        # a live feed saying unplugged since the plug, or an unplug just
        # before it, rules the car out
        if telemetryPlug(v, since) is False:
            return False
        if v.markAt("unplug") > since - REACH_WINDOW > NEVER:
            return False
        ble = read and "TeslaBLE" in v.claims and not fresh(v, "chargingState")
        if ble and asleep(v, now - REACH_WINDOW) is None:
            # A charge read may wake the car: the sleep-safe check first
            master.providerOrder.provider("TeslaBLE").module.ping(v.vin)
        if ble and asleep(v, now - REACH_WINDOW) is False:
            v.read("chargingState", 0, providers=("TeslaBLE",), force=True)
        state, pilot, amps = (fresh(v, f) for f in _BLE_CHARGE)
        plugged = state and _plug("chargingState", state)
        if not (plugged and pilot and offered is not None):
            return None if plugged else plugged
        # The offer may have changed during the read
        gap = min(abs(pilot.value - o) for o in (offered, offer()))
        if gap > PILOT_MATCH_TOLERANCE:
            return False
        drawing = actual >= 1 and amps
        return not drawing or abs(amps.value - actual) <= PILOT_MATCH_TOLERANCE or None

    fit = [(v, fits(v)) for v in pool]
    left = [v for v, f in fit if f is not False]
    won = [v for v, f in fit if f] or [v for v in left if _reached(master, v)]
    won = won or [v for v in left if inside(master, v)]
    return won[0] if len(won) == 1 else None


def attribute(master, pointId, source, offered=None, actual=0) -> Optional[str]:
    # Names an anonymous session's car, plugged in there. Given an offer it
    # may read over BLE, so it runs on the BLE worker
    if not getattr(anon := master.vehicleRegistry.vehicleAt(pointId), "isAnonymous", 0):
        return None
    read, plugIn = offered is not None, anon.markAt("plugIn")
    v = pick(master, offered, actual, read, plugIn)
    if v is None and read and not candidates(master) and anon.markAt("scan") < plugIn:
        # A plug-in is arrival evidence for some car: one scan of every car,
        # once per plug-in, makes one the radio hears a candidate (its
        # contact says home). Passive: it wakes nothing
        anon.mark("scan", plugIn)
        ble = master.getModuleByName("TeslaBLE")
        cars = [c.vin for c in master.vehicleRegistry.vehicles() if c.vin]
        if ble and cars and ble.scanForVehicleInRange(cars):
            v = pick(master, offered, actual, read, plugIn)
    if v is None:
        # Once per plug-in: news of a car attributes it again
        if anon.markAt("leftAnonymous") < plugIn:
            anon.mark("leftAnonymous", plugIn)
            logger.info("%s: no single car plugged in; left anonymous", pointId)
        return None
    master.vehicleRegistry.observeTwcVin(pointId, v.vin)
    v.observe("pluggedIn", True, source)
    return v.vin


def homeCars(master, attachment) -> List[Tuple[Any, Any]]:
    pairs = master.vehicleRegistry.attached(attachment)
    return [(v, a) for v, a in pairs if presence(master, v) != NOT_HOME]


def learnHome(master, vehicle, lat, lon) -> bool:
    # A car a TWC names, or BLE reads, is here: a fresh fix outside the
    # radius, or with no home yet, moves home to it. A stale fix may be
    # wherever the car last had reception: the GPS time its source gave
    # (observed before the position) dates it, else its reading's time. A
    # retained or redelivered copy is never a fix, whatever its stamp. A fix
    # not read over BLE must be read after the car reached its TWC (the later
    # of its plug-in and the VIN read; a Gen2's plug-in mark stays on the
    # anonymous session the VIN read replaces), else it may predate the drive
    # home
    if lat is None or lon is None or not (lat.seen and lon.seen):
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
