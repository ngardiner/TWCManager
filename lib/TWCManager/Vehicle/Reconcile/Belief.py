# What the reconciler believes about a car, and the per-aspect rules it
# plans by: pure functions of the registry, no reconciler state. The EVSE
# layer reads known() from here without the reconciler thread.
from typing import Optional

from ..Registry.Fields import FEED_LAG
from ..Presence import NOT_HOME, pluggedNow, presence
from .Desired import AMPS, CHARGE_LIMIT, CHARGING, FIELD_FOR
from .Rules import releases, VEHICLE_SOURCES_EXCLUDED

# Sources whose every reading came from the car itself. Exact source
# names; Fleet Telemetry always counts
LIVE_READ_SOURCES = frozenset(
    {"TeslaBLE", "OwnerAPI", "FleetAPI", "FleetTelemetryMQTT"}
)
# TWC current at or above this means the car is drawing power
POWER_FLOWING_AMPS = 1.0


def _newer(vehicle, field, since, twc=False):
    # Each vehicle (or TWC) source's own newest reading of field after since:
    # a republish by one source never hides another's live read
    return [
        obs
        for source, obs in vehicle.reported(field).items()
        if (source in VEHICLE_SOURCES_EXCLUDED) == twc
        and obs.at > since
        and obs.value is not None
    ]


def standIn(vehicle):
    # The newest TWC current, as the car's charging state
    reads = _newer(vehicle, "actualCurrent", float("-inf"), twc=True)
    if not reads:
        return None
    obs = max(reads, key=lambda r: r.at)
    flowing = obs.value >= POWER_FLOWING_AMPS
    return obs._replace(value="Charging" if flowing else "Stopped")


def uncompleted(vehicle, obs):
    # "Complete" read before our last limit write above the battery level
    # is no longer true: the car can charge again
    write = vehicle.lastWrite(FIELD_FOR[CHARGE_LIMIT])
    soc = vehicle.latest("batteryLevel")
    if obs is None or obs.value != "Complete" or write is None or soc is None:
        return obs
    raised = obs.at <= write.at and soc.value < write.value
    return obs._replace(value="Stopped") if raised else obs


def known(vehicle, aspect, desired=None):
    obs = vehicle.believed(FIELD_FOR[aspect], exclude=VEHICLE_SOURCES_EXCLUDED)
    if aspect != CHARGING:
        return obs
    obs, twc = uncompleted(vehicle, obs), standIn(vehicle)
    if obs is None:
        # A car no vehicle source describes (anonymous, non-Tesla): the TWC
        # current stands in. It never adopts
        return twc
    # For a start, current at the car's TWC now (a heartbeat within
    # FEED_LAG) and since its last reading is the car charging: without a
    # live feed that reading may be stale
    fresh = twc and twc.at > max(obs.at, vehicle._clock() - FEED_LAG)
    if desired is True and fresh and twc.value == "Charging":
        return twc
    return obs


def newerReading(vehicle, aspect, since) -> bool:
    field = FIELD_FOR[aspect]
    if aspect == CHARGING and vehicle.believed(field, VEHICLE_SOURCES_EXCLUDED) is None:
        # Only while known uses the stand-in: for a car a vehicle source
        # describes, the heartbeat's readings are never evidence
        obs = standIn(vehicle)
        return obs is not None and obs.at > since
    return bool(_newer(vehicle, field, since))


def reachedCar(vehicle, field, since, beliefAtSend) -> bool:
    # A live read, or a value unlike the one believed at the send. HA or
    # TeslaMate republishing an unchanged value is no evidence
    return any(
        obs.source in LIVE_READ_SOURCES or obs.value != beliefAtSend
        for obs in _newer(vehicle, field, since)
    )


def handsOff(vehicle, aspect, desired, known, config=None) -> Optional[str]:
    if aspect != CHARGING:
        return None
    config = config or {}
    battery = vehicle.value("batteryLevel")
    if desired is False:
        if battery is not None and battery < config.get("minChargeLevel", -1):
            # A car below the minimum is never stopped
            return "battery below minChargeLevel"
        return None
    # Our own write counts until a newer reading
    limit = vehicle.believed("chargeLimit", VEHICLE_SOURCES_EXCLUDED)
    limit = limit.value if limit is not None else None
    if battery and limit and battery >= limit:
        # The car would refuse the start
        return "battery at its charge limit"
    return _refusesStart(vehicle, known, config)


def _refusesStart(vehicle, known, config) -> Optional[str]:
    # Whatever its limit, the car cannot take a start, or must not get one
    if known is not None and known.value in ("Disconnected", "NoPower"):
        return "the car cannot charge (%s)" % known.value
    if vehicle.value("scheduledChargingPending") is True and config.get(
        "respectVehicleSchedule", True
    ):
        # A start would override the car's own schedule
        return "scheduled charging is pending (respectVehicleSchedule)"
    return None


def _wontCharge(master, vehicle) -> Optional[str]:
    # Why the car would not charge if started now, once every pending desire
    # is applied; None when it would (unknown limit or battery: it would)
    key, desired = vehicle.key, master.desired
    stop, pending = desired.get(key, CHARGING), desired.get(key, CHARGE_LIMIT)
    held = master.heldLimits.get(vehicle.vin) if vehicle.vin else None
    limit, battery = known(vehicle, CHARGE_LIMIT), vehicle.value("batteryLevel")
    limit = limit and limit.value
    if pending is not None and not (releases(pending) and not held):
        limit = held.outside if releases(pending) else pending.value
    # Unplugged is known only by a reading
    plugged = pluggedNow(master, vehicle, None)
    if presence(master, vehicle) == NOT_HOME or plugged is False:
        return "not plugged in here"
    if stop is not None and stop.value is False:
        return "stop desired"
    if limit and battery and limit <= battery:
        return "car won't charge: limit at or below battery"
    config = (getattr(master, "config", None) or {}).get("config") or {}
    return _refusesStart(vehicle, known(vehicle, CHARGING), config)


def wouldCharge(master, vehicle) -> bool:
    return _wontCharge(master, vehicle) is None


def _room(master, vehicle) -> bool:
    # The policy's power, less the minimums of the points already drawing
    # (the split's own test), fits this car's point: a drawing car keeps its
    # place, and a new car is admitted only into real headroom
    own, idle = [], [1]
    free = master.convertAmpsToWatts(master.getMaxAmpsToDivideAmongSlaves())
    for point in master.chargingPoints():
        # A TWC still holding the car is its point while it reports no car
        held = (e.heldKey for e in point.localEndpoints)
        if vehicle.key in (point.vehicleKey, *held):
            own.append(point.minPower)
        elif point.wantsToCharge and not point.isReadOnly:
            free -= point.minPower
        else:
            idle.append(point.minPower)
    # A car with no point of its own may be on any idle one
    return free >= max(own or idle)


def admissible(master, vehicle) -> bool:
    # The admission a split that admits before it allocates can share
    return wouldCharge(master, vehicle) and _room(master, vehicle)


def whyQuiet(master, vehicle) -> Optional[str]:
    # Why the car is expected to quiesce, or None when it will charge
    reason = _wontCharge(master, vehicle)
    if reason or not _room(master, vehicle):
        return reason or "no room to start"
    feeds = map(master.getModuleByName, ("TeslaMateVehicle", "FleetTelemetryMQTT"))
    if any(f and f.registry.now() < f.settleUntil for f in feeds):
        # Its first readings may show a full car
        return "waiting for telemetry after start"
    return None
