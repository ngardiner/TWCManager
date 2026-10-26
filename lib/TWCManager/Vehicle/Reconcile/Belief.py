# What the reconciler believes about a car, and the per-aspect rules it
# plans by: pure functions of the registry, no reconciler state. The EVSE
# layer reads known() from here without the reconciler thread.
from typing import Optional

from .Desired import AMPS, CHARGE_LIMIT, CHARGING, FIELD_FOR
from .Rules import VEHICLE_SOURCES_EXCLUDED

# Sources whose every reading came from the car itself (c2). Exact source
# names; Fleet Telemetry always counts (U2)
LIVE_READ_SOURCES = frozenset(
    {"TeslaBLE", "OwnerAPI", "FleetAPI", "FleetTelemetryMQTT"}
)
# TWC current at or above this means the car is drawing power
POWER_FLOWING_AMPS = 1.0


def _newer(vehicle, field, since, twc=False):
    # Each vehicle (or TWC) source's own newest reading of field after since:
    # a republish by one source never hides another's live read (I3)
    return [
        obs
        for source, obs in vehicle.reported(field).items()
        if (source in VEHICLE_SOURCES_EXCLUDED) == twc
        and obs.at > since
        and obs.value is not None
    ]


def _standIn(vehicle):
    # The newest TWC current, as the car's charging state
    reads = _newer(vehicle, "actualCurrent", float("-inf"), twc=True)
    if not reads:
        return None
    obs = max(reads, key=lambda r: r.at)
    flowing = obs.value >= POWER_FLOWING_AMPS
    return obs._replace(value="Charging" if flowing else "Stopped")


def uncompleted(vehicle, obs):
    # "Complete" read before our last limit write above the battery level
    # is no longer true: the car can charge again (main's start went out)
    write = vehicle.lastWrite(FIELD_FOR[CHARGE_LIMIT])
    soc = vehicle.latest("batteryLevel")
    if obs is None or obs.value != "Complete" or write is None or soc is None:
        return obs
    raised = obs.at <= write.at and soc.value < write.value
    return obs._replace(value="Stopped") if raised else obs


def known(vehicle, aspect):
    obs = vehicle.believed(FIELD_FOR[aspect], exclude=VEHICLE_SOURCES_EXCLUDED)
    if aspect == CHARGING:
        obs = uncompleted(vehicle, obs)
    if obs is None and aspect == CHARGING:
        # A car no vehicle source describes (anonymous, non-Tesla): the TWC
        # current stands in. It never adopts
        return _standIn(vehicle)
    return obs


def newerReading(vehicle, aspect, since) -> bool:
    field = FIELD_FOR[aspect]
    if aspect == CHARGING and vehicle.believed(field, VEHICLE_SOURCES_EXCLUDED) is None:
        # Only while known uses the stand-in: for a car a vehicle source
        # describes, the heartbeat's readings are never evidence
        obs = _standIn(vehicle)
        return obs is not None and obs.at > since
    return bool(_newer(vehicle, field, since))


def reachedCar(vehicle, field, since, beliefAtSend) -> bool:
    # A live read, or a value unlike the one believed at the send. HA or
    # TeslaMate republishing an unchanged value is no evidence (c2)
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
            # As main: a car below the minimum is never stopped
            return "battery below minChargeLevel"
        return None
    # Our own write counts until a newer reading
    limit = vehicle.believed("chargeLimit", VEHICLE_SOURCES_EXCLUDED)
    limit = limit.value if limit is not None else None
    if battery and limit and battery >= limit:
        # As main (#591): the car would refuse the start
        return "battery at its charge limit"
    if known is not None and known.value in ("Disconnected", "NoPower"):
        return "the car cannot charge (%s)" % known.value
    if vehicle.value("scheduledChargingPending") is True and config.get(
        "respectVehicleSchedule", True
    ):
        # A start would override the car's own schedule (as on main)
        return "scheduled charging is pending (respectVehicleSchedule)"
    return None


def allowWake(aspect, desired, known) -> bool:
    # A raise or a start; never a lower, a stop or a restore. A billed
    # provider never wakes for amps (the caller's rule)
    if desired.restore:
        return False
    if aspect == CHARGE_LIMIT:
        try:
            return known is not None and desired.value > known.value
        except TypeError:
            return False
    return desired.value is True if aspect == CHARGING else aspect == AMPS
