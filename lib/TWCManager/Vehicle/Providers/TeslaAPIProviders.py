# The TeslaAPI module's two token slots as two providers. The Owner API reads
# for free, and commands and wakes only a car Tesla lists with
# command_signing "off", while no Fleet token commands. The Fleet API bills
# each vehicle_data, command and wake, so it reads only when the Owner slot
# cannot (slotFor("read")). Neither does I/O to answer ready or available.
# Both read the energy site's Wall Connector VINs for free, Owner first.
from typing import Dict, Optional, Tuple

from .Capability import (
    CLOUD_FREE,
    COMMANDS,
    FLEET_COMMAND,
    FLEET_DATA,
    FLEET_WAKE,
    OWNER_COMMAND,
    READ_CHARGE,
    READ_LIST,
    READ_LOCATION,
    READ_ONLINE,
    READ_WALL_CONNECTOR_VINS,
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
# One connect-edge query plus one poll a minute
WALL_CONNECTOR_VINS = Declaration(CLOUD_FREE, RateLimit(2, 60))

# The Tesla command each capability sends, for the read-only line
TESLA_COMMAND = {
    SET_CHARGE_LIMIT: "set_charge_limit",
    START_CHARGING: "charge_start",
    STOP_CHARGING: "charge_stop",
    SET_AMPS: "set_charging_amps",
    WAKE: "wake_up",
}


class OwnerAPIProvider(Provider):
    name = "OwnerAPI"
    rank = 4
    local = False

    def __init__(self, carapi):
        self.carapi = carapi
        self.module = carapi
        self.readSource = carapi.ownerReadSource

    def declarations(self):
        declared = {
            capability: Declaration(CLOUD_FREE)
            for capability in (READ_LIST, READ_ONLINE, READ_CHARGE, READ_LOCATION)
        }
        declared.update(
            {capability: Declaration(OWNER_COMMAND) for capability in COMMANDS}
        )
        declared[READ_WALL_CONNECTOR_VINS] = WALL_CONNECTOR_VINS
        return declared

    def ready(self, vehicle, capability) -> bool:
        if capability == READ_WALL_CONNECTOR_VINS:
            # A site read: no vehicle needed
            return self.carapi.wallConnectorVinsReady(self.carapi.ownerSlot)
        if vehicle is None or capability not in self.declarations():
            return False
        car = vehicle.attachment("TeslaAPI")
        if car is None:
            return False
        if capability in COMMANDS:
            # Says once why a car is read-only, as the module would have
            return self.available(capability) and not self.carapi.refuseCommandFor(
                car, TESLA_COMMAND[capability]
            )
        return self.carapi.ownerSlot.usable("read")

    def available(self, capability) -> bool:
        # The broadcast gate for commands: an Owner token that may command
        # some listed car, or whose cars are not listed yet, while no Fleet
        # token commands (TeslaAPI then sends through the Fleet slot)
        if capability in COMMANDS:
            return (
                not self.carapi.fleetSlot.usable("command") and not self.carapi.readOnly
            )
        return True

    def wallConnectorVins(self) -> Optional[Dict[str, str]]:
        return self.carapi.wallConnectorVins(self.carapi.ownerSlot)


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
            READ_WALL_CONNECTOR_VINS: WALL_CONNECTOR_VINS,
        }

    def ready(self, vehicle, capability) -> bool:
        if capability == READ_WALL_CONNECTOR_VINS:
            # The read-slot rule: Fleet only when the Owner slot cannot
            return self.carapi.wallConnectorVinsReady(
                self.carapi.fleetSlot
            ) and not self.carapi.wallConnectorVinsReady(self.carapi.ownerSlot)
        if vehicle is None or capability not in self.declarations():
            return False
        if vehicle.attachment("TeslaAPI") is None:
            return False
        return self.available(capability) and (
            capability in COMMANDS or not self.carapi.ownerSlot.usable("read")
        )

    def available(self, capability) -> bool:
        # The broadcast gate: without a Fleet command token no vehicle-less
        # command comes here (an Owner token's commands go per car, through
        # the OwnerAPI provider). While no listed car can be commanded, the
        # module says once why, as it would when called.
        if capability in COMMANDS:
            usable = self.carapi.fleetSlot.usable("command")
            if not usable:
                self.carapi.refuseCommandIfReadOnly(TESLA_COMMAND[capability])
            return usable
        if capability == READ_WALL_CONNECTOR_VINS:
            return self.carapi.wallConnectorVinsReady(self.carapi.fleetSlot)
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

    def wallConnectorVins(self) -> Optional[Dict[str, str]]:
        return self.carapi.wallConnectorVins(self.carapi.fleetSlot)
