# Per-vehicle fields: each holds its latest observation (value, source, time)
# and the last value we wrote ourselves, so readers can tell a stale feed from
# a current one and an echo of our own write from news. The tables live on
# Vehicle.
from typing import Any, NamedTuple


class Observation(NamedTuple):
    value: Any
    source: str
    at: float
    seen: bool = True  # False: a retained redelivery, not a sighting


FIELDS = (
    "name",
    "batteryLevel",
    "chargeLimit",
    "chargingState",
    "pluggedIn",
    "state",
    "lat",
    "lon",
    "gpsAsOf",
    "actualCurrent",
    "availableCurrent",
    # The car's own charge-current setting (amps), not the charger's pilot
    "chargeCurrentRequest",
    "voltage",
    "phases",
    "timeToFullCharge",
    "scheduledChargingPending",
    "twcVin",
    "bleReach",
    "shiftState",
    "fastChargerPresent",
    "fastChargerType",
    "connChargeCable",
    # Charging power (kW) and the car's own AC current maximum (amps)
    "chargerPower",
    "chargeCurrentRequestMax",
    # The charge-port door, which VCSEC reports only while the car is awake
    "chargePortOpen",
)

# Source names are module class names. A source is report-on-change exactly
# when it has registered liveness with the registry.
TELEMETRY_SOURCES = frozenset({"TeslaMateVehicle", "FleetTelemetryMQTT"})

# How long a healthy report-on-change feed gets to report a change after one
# of our own writes before its silence counts as "the write did not land".
FEED_LAG = 30.0

# A read (TeslaAPI's update_charge, the reconciler's read before a write)
# accepts a value this old (seconds)
READ_MAX_AGE = 300.0

# Sentinel for age()/trusted(): the observation's source is polled, not a
# report-on-change feed.
NOT_TRACKED = object()
