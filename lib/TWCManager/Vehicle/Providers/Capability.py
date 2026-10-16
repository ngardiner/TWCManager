# The vocabulary a provider declares itself in: what it can do, what each
# operation costs, and how often it may be asked. It imports nothing.
from typing import NamedTuple, Optional

# Reads: a vehicle's charge state and location (one fetch fills both)
READ_CHARGE = "readCharge"
# An energy-site read: which VIN each Wall Connector on the account reports
READ_WALL_CONNECTOR_VINS = "wallConnectorVins"

# The registry fields a READ_CHARGE fetch can fill
CHARGE_FIELDS = frozenset(
    {
        "batteryLevel",
        "chargeLimit",
        "chargingState",
        "availableCurrent",
        "chargeCurrentRequest",
        "actualCurrent",
        "voltage",
        "phases",
        "scheduledChargingPending",
        "timeToFullCharge",
        "chargeCurrentRequestMax",
    }
)
LOCATION_FIELDS = frozenset({"lat", "lon", "gpsAsOf"})

# Commands
SET_CHARGE_LIMIT = "setChargeLimit"
START_CHARGING = "startCharging"
STOP_CHARGING = "stopCharging"
SET_AMPS = "setAmps"
WAKE = "wake"

READS = frozenset({READ_CHARGE, READ_WALL_CONNECTOR_VINS})
COMMANDS = frozenset({SET_CHARGE_LIMIT, START_CHARGING, STOP_CHARGING, SET_AMPS, WAKE})
ALL = READS | COMMANDS

# Cost tiers, cheapest first
TIER_LOCAL = 0
TIER_CLOUD_FREE = 1
TIER_BILLED = 2
TIER_DISRUPTIVE = 3


class Cost(NamedTuple):
    tier: int
    price: Optional[float] = None  # USD per operation; None = unknown


LOCAL = Cost(TIER_LOCAL, 0.0)
CLOUD_FREE = Cost(TIER_CLOUD_FREE, 0.0)
FLEET_COMMAND = Cost(TIER_BILLED, 0.001)
FLEET_DATA = Cost(TIER_BILLED, 0.002)
FLEET_WAKE = Cost(TIER_BILLED, 0.02)
# An Owner API command or wake costs nothing, but stands in for Fleet and
# Tesla rate-limits it: paced as the billed tier is
OWNER_COMMAND = Cost(TIER_BILLED, 0.0)
HA_BILLED = Cost(TIER_BILLED, None)
DISRUPTIVE = Cost(TIER_DISRUPTIVE, 0.0)


# A limitation a provider may declare per vehicle: a stop through this
# provider also affects other endpoints (Gen3 units on one bus).
NO_INDIVIDUAL_STOP = "noIndividualStop"


class Declaration(NamedTuple):
    cost: Cost
