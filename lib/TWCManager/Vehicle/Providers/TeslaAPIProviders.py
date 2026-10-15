# The TeslaAPI module's two token slots as two providers. The Owner API reads
# for free and never commands. The Fleet API bills each vehicle_data, command
# and wake, so it reads only when the Owner slot cannot (slotFor("read")), and
# it alone commands and wakes. Neither does I/O to answer ready or available.
from typing import Optional, Tuple

from .Capability import (
    CLOUD_FREE,
    COMMANDS,
    FLEET_COMMAND,
    FLEET_DATA,
    FLEET_WAKE,
    READ_CHARGE,
    READ_LIST,
    READ_LOCATION,
    READ_ONLINE,
    SET_AMPS,
    SET_CHARGE_LIMIT,
    START_CHARGING,
    STOP_CHARGING,
    WAKE,
    Declaration,
    RateLimit,
)
from .Provider import Provider, vehicleMaxAmps

# Tesla's published per-vehicle command limit; declared, not enforced
FLEET_COMMAND_LIMIT = RateLimit(50, 60)


class OwnerAPIProvider(Provider):
    name = "OwnerAPI"
    rank = 4
    local = False

    def __init__(self, carapi):
        self.carapi = carapi
        self.module = carapi
        self.readSource = carapi.ownerReadSource

    def declarations(self):
        return {
            capability: Declaration(CLOUD_FREE)
            for capability in (READ_LIST, READ_ONLINE, READ_CHARGE, READ_LOCATION)
        }

    def ready(self, vehicle, capability) -> bool:
        if vehicle is None or capability not in self.declarations():
            return False
        return (
            self.carapi.ownerSlot.usable("read")
            and vehicle.attachment("TeslaAPI") is not None
        )


class FleetAPIProvider(Provider):
    name = "FleetAPI"
    rank = 5
    local = False

    def __init__(self, carapi):
        self.carapi = carapi
        self.module = carapi
        self.readSource = carapi.fleetReadSource

    def declarations(self):
        return {
            READ_LIST: Declaration(CLOUD_FREE),
            READ_ONLINE: Declaration(CLOUD_FREE),
            READ_CHARGE: Declaration(FLEET_DATA),
            READ_LOCATION: Declaration(FLEET_DATA),
            SET_CHARGE_LIMIT: Declaration(FLEET_COMMAND, FLEET_COMMAND_LIMIT),
            START_CHARGING: Declaration(FLEET_COMMAND, FLEET_COMMAND_LIMIT),
            STOP_CHARGING: Declaration(FLEET_COMMAND, FLEET_COMMAND_LIMIT),
            SET_AMPS: Declaration(FLEET_COMMAND, FLEET_COMMAND_LIMIT),
            WAKE: Declaration(FLEET_WAKE),
        }

    def ready(self, vehicle, capability) -> bool:
        if vehicle is None or capability not in self.declarations():
            return False
        if vehicle.attachment("TeslaAPI") is None:
            return False
        return self.available(capability) and (
            capability in COMMANDS or not self.carapi.ownerSlot.usable("read")
        )

    def available(self, capability) -> bool:
        # The broadcast gate: an Owner-only install never sends a
        # vehicle-less command here, as today's read-only refusal skips it
        if capability in COMMANDS:
            return self.carapi.fleetSlot.usable("command")
        return self.carapi.fleetSlot.usable("read")

    def ampsRange(self, vehicle) -> Optional[Tuple[float, float]]:
        if vehicle is None:
            return None
        return (1, vehicleMaxAmps(vehicle, self.carapi.master.config["config"]))

    def setAmps(self, vin: str, amps: int) -> bool:
        car = self.carapi.getVehicleByVIN(vin)
        if not car:
            return False
        return bool(self.carapi.setChargeRate(int(amps), vehicle=car))

    def wake(self, vin: str) -> bool:
        car = self.carapi.getVehicleByVIN(vin)
        if not car:
            return False
        return bool(self.carapi.wakeVehicle(car))
