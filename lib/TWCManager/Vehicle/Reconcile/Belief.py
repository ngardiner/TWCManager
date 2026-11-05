# What the reconciler believes about a car, and the per-aspect rules it
# plans by: pure functions of the registry, no reconciler state. The EVSE
# layer reads known() from here without the reconciler thread.
from typing import Optional

from ..Registry.Fields import FEED_LAG
from ..Presence import NOT_HOME, pluggedNow, presence
from .Desired import AMPS, CHARGE_LIMIT, CHARGING, FIELD_FOR
from .Rules import releases, TWC_SOURCES

# Sources whose every reading came from the car itself. Exact source
# names; Fleet Telemetry always counts
LIVE_READ_SOURCES = frozenset(
    {"TeslaBLE", "OwnerAPI", "FleetAPI", "FleetAPICached", "FleetTelemetryMQTT"}
)
# TWC current at or above this means the car is drawing power
POWER_FLOWING_AMPS = 1.0


def newerReadings(vehicle, field, since, twc=False):
    # Each vehicle (or TWC) source's own newest reading of field after since:
    # a republish by one source never hides another's live read
    return [
        obs
        for source, obs in vehicle.reported(field).items()
        if (source in TWC_SOURCES) == twc and obs.at > since and obs.value is not None
    ]


def twcChargingState(vehicle):
    # The newest TWC current, as the car's charging state
    reads = newerReadings(vehicle, "actualCurrent", float("-inf"), twc=True)
    if not reads:
        return None
    obs = max(reads, key=lambda r: r.at)
    flowing = obs.value >= POWER_FLOWING_AMPS
    return obs._replace(value="Charging" if flowing else "Stopped")


def unlessLimitRaised(vehicle, obs):
    # "Complete" read before our last limit write above the battery level
    # (or with the level unknown) is no longer true: the car can charge again
    write = vehicle.lastWrite(FIELD_FOR[CHARGE_LIMIT])
    soc = vehicle.latest("batteryLevel")
    if obs is None or obs.value != "Complete" or write is None:
        return obs
    raised = obs.at <= write.at and (soc is None or soc.value < write.value)
    return obs._replace(value="Stopped") if raised else obs


def known(vehicle, aspect, desired=None):
    obs = vehicle.believed(FIELD_FOR[aspect], exclude=TWC_SOURCES)
    if aspect != CHARGING:
        return obs
    obs, twc = unlessLimitRaised(vehicle, obs), twcChargingState(vehicle)
    if obs is None:
        # A car no vehicle source describes (anonymous, non-Tesla): the TWC
        # current stands in. It never adopts
        return twc
    # For a start, the TWC current wins when it is recent (a heartbeat
    # within FEED_LAG), newer than the car's last reading, and flowing.
    # Without a live feed the car's reading may be stale.
    fresh = twc and twc.at > max(obs.at, vehicle._clock() - FEED_LAG)
    if desired is True and fresh and twc.value == "Charging":
        return twc
    return obs


def newerReading(vehicle, aspect, since) -> bool:
    field = FIELD_FOR[aspect]
    if aspect == CHARGING and vehicle.believed(field, TWC_SOURCES) is None:
        # Only while known uses the TWC's charging state: for a car a vehicle
        # source describes, the heartbeat's readings are never evidence
        obs = twcChargingState(vehicle)
        return obs is not None and obs.at > since
    return bool(newerReadings(vehicle, field, since))


def reachedCar(vehicle, field, since, beliefAtSend) -> bool:
    # A live read, or a value unlike the one believed at the send. HA or
    # TeslaMate republishing an unchanged value is no evidence
    return any(
        obs.source in LIVE_READ_SOURCES or obs.value != beliefAtSend
        for obs in newerReadings(vehicle, field, since)
    )


def whyHandsOff(vehicle, aspect, desired, believed, config=None) -> Optional[str]:
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
    limit = vehicle.believed("chargeLimit", TWC_SOURCES)
    limit = limit.value if limit is not None else None
    if battery and limit and battery >= limit:
        # The car would refuse the start
        return "battery at its charge limit"
    return _refusesStart(vehicle, believed, config)


def _refusesStart(vehicle, believed, config) -> Optional[str]:
    # Whatever its limit, the car cannot take a start, or must not get one
    if believed is not None and believed.value in ("Disconnected", "NoPower"):
        return "the car cannot charge (%s)" % believed.value
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
    pending = desired.get(key, CHARGE_LIMIT)
    saved = master.heldLimits.get(vehicle.vin) if vehicle.vin else None
    battery = vehicle.value("batteryLevel")
    belief = known(vehicle, CHARGE_LIMIT)
    believed = belief.value if belief is not None else None
    state = known(vehicle, CHARGING)
    # The limit once pending is applied: a release puts back the saved
    # outside limit, and with none saved leaves the car's own
    limit = believed
    if pending is not None and not releases(pending):
        limit = pending.value
    elif pending is not None and saved is not None:
        limit = saved.outside
    # Unplugged is known only by a reading
    plugged = pluggedNow(master, vehicle, ifUnknown=None)
    if presence(master, vehicle) == NOT_HOME or plugged is False:
        return "not plugged in here"
    if limit and battery and limit <= battery:
        return "car won't charge: limit at or below battery"
    if getattr(state, "value", None) == "Complete" and limit == believed:
        return "car won't charge: complete at its limit"
    config = (getattr(master, "config", None) or {}).get("config") or {}
    return _refusesStart(vehicle, state, config)


def wouldCharge(master, vehicle) -> bool:
    return _wontCharge(master, vehicle) is None


def whyQuiet(master, vehicle) -> Optional[str]:
    # Why the car is expected to stay quiet, or None when it will charge.
    # Room to start is the control tick's admission set, which the tick
    # replaces whole rather than changing in place.
    reason = _wontCharge(master, vehicle)
    if reason or vehicle.key not in getattr(master, "admission", ()):
        return reason or "no room to start"
    feeds = map(master.getModuleByName, ("TeslaMateVehicle", "FleetTelemetryMQTT"))
    if any(f and f.registry.now() < f.settleUntil for f in feeds):
        # Its first readings may show a full car
        return "waiting for telemetry after start"
    return None
