# The TeslaAPI module's two token slots as two providers. The Owner API reads
# for free and never commands. The Fleet API bills each vehicle_data, command
# and wake, so it reads only when the Owner slot cannot (slotFor("read")), and
# it alone commands and wakes. Neither does I/O to answer ready or available.
# Both read the energy site's Wall Connector VINs for free, Owner first.
import time
from typing import Dict, Optional, Tuple

from ..Reconcile.Outcome import SATISFIED, SETTLED, UNSUPPORTED, Outcome

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
    READ_WALL_CONNECTOR_VINS,
    SET_AMPS,
    SET_CHARGE_LIMIT,
    START_CHARGING,
    STOP_CHARGING,
    WAKE,
    Declaration,
)
from .Provider import CLOUD_SUPPRESSED, Provider, vehicleMaxAmps

# The reader paces it: a connect edge, then one poll a minute
WALL_CONNECTOR_VINS = Declaration(CLOUD_FREE)


class OwnerAPIProvider(Provider):
    # Declares no commands: the inherited command methods answer UNSUPPORTED
    name = "OwnerAPI"
    rank = 4
    local = False
    DECLARATIONS = {
        READ_LIST: Declaration(CLOUD_FREE),
        READ_ONLINE: Declaration(CLOUD_FREE),
        READ_CHARGE: Declaration(CLOUD_FREE),
        READ_LOCATION: Declaration(CLOUD_FREE),
        READ_WALL_CONNECTOR_VINS: WALL_CONNECTOR_VINS,
    }
    SUPPRESSED = CLOUD_SUPPRESSED

    def __init__(self, carapi):
        self.carapi = carapi
        self.module = carapi

    def ready(self, vehicle, capability) -> bool:
        if capability == READ_WALL_CONNECTOR_VINS:
            # A site read: no vehicle needed
            return self.carapi.wallConnectorVinsReady(self.carapi.ownerSlot)
        if vehicle is None or capability not in self.declarations():
            return False
        return (
            self.carapi.ownerSlot.usable("read")
            and vehicle.attachment("TeslaAPI") is not None
        )

    def wallConnectorVins(self) -> Optional[Dict[str, str]]:
        return self.carapi.wallConnectorVins(self.carapi.ownerSlot)


class FleetAPIProvider(Provider):
    name = "FleetAPI"
    rank = 5
    local = False
    DECLARATIONS = {
        READ_LIST: Declaration(CLOUD_FREE),
        READ_ONLINE: Declaration(CLOUD_FREE),
        READ_CHARGE: Declaration(FLEET_DATA),
        READ_LOCATION: Declaration(FLEET_DATA),
        SET_CHARGE_LIMIT: Declaration(FLEET_COMMAND),
        START_CHARGING: Declaration(FLEET_COMMAND),
        STOP_CHARGING: Declaration(FLEET_COMMAND),
        SET_AMPS: Declaration(FLEET_COMMAND),
        WAKE: Declaration(FLEET_WAKE),
        READ_WALL_CONNECTOR_VINS: WALL_CONNECTOR_VINS,
    }
    SUPPRESSED = CLOUD_SUPPRESSED

    def __init__(self, carapi):
        self.carapi = carapi
        self.module = carapi

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
        # The broadcast gate: an Owner-only install never sends a
        # vehicle-less command here, as today's read-only refusal skips it
        if capability in COMMANDS:
            return self.carapi.fleetSlot.usable("command")
        if capability == READ_WALL_CONNECTOR_VINS:
            return self.carapi.wallConnectorVinsReady(self.carapi.fleetSlot)
        return self.carapi.fleetSlot.usable("read")

    def ampsRange(self, vehicle) -> Optional[Tuple[float, float]]:
        if vehicle is None:
            return None
        return (1, vehicleMaxAmps(vehicle, self.carapi.master.config["config"]))

    # Command methods: one billed request each (set_charging_amps below 5 A
    # excepted, as TeslaPy #42), classified by TeslaAPI.command. No pre-wake:
    # the lane wakes first when the attempt says so.

    # The clock passed to the wake hold; a test may replace it
    clock = time.time

    def _car(self, vin: str):
        # The account's CarApiVehicle for vin, or None
        return self.carapi.getVehicleByVIN(vin) or None

    def _command(self, vin: str, name: str, body: Optional[dict] = None) -> Outcome:
        car = self._car(vin)
        if car is None:
            return Outcome(UNSUPPORTED, "not listed")
        return self.carapi.command(car, name, body)

    def setChargeLimit(self, vin: str, limit: int) -> Outcome:
        return self._command(vin, "set_charge_limit", {"percent": int(limit)})

    def startCharging(self, vin: str) -> Outcome:
        outcome = self._command(vin, "charge_start")
        if outcome == Outcome(SATISFIED, "disconnected"):
            # Nothing to start: not a start
            return Outcome(UNSUPPORTED, "disconnected")
        return outcome

    def stopCharging(self, vin: str) -> Outcome:
        car = self._car(vin)
        if (
            car is not None
            and self.carapi.slotFor("command") is not None
            and not car.is_awake(now=self.clock())
            and car.lastVehicleAwakeKnown
        ):
            # As main: an asleep car is not charging; settled, never sent (N2)
            return Outcome(SATISFIED, "asleep, not charging")
        return self._command(vin, "charge_stop")

    def setAmps(self, vin: str, amps: int) -> Outcome:
        # Vehicle amps are never 0: 0 would stop the car charging
        body = {"charging_amps": max(1, int(amps))}
        outcome = self._command(vin, "set_charging_amps", body)
        if body["charging_amps"] < 5 and outcome.kind in SETTLED:
            # A rate below 5 A takes a second identical request (TeslaPy #42)
            time.sleep(5)
            outcome = self._command(vin, "set_charging_amps", body)
        return outcome

    def wake(self, vin: str) -> Outcome:
        car = self._car(vin)
        if car is None:
            return Outcome(UNSUPPORTED, "not listed")
        return self.carapi.wakeOutcome(car, now=self.clock())

    def wallConnectorVins(self) -> Optional[Dict[str, str]]:
        return self.carapi.wallConnectorVins(self.carapi.fleetSlot)
