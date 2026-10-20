# The TeslaAPI module's two token slots as two providers. The Owner API reads
# for free, and commands and wakes only a car Tesla lists with
# command_signing "off", and ranks first (canCommand). The Fleet API
# bills each vehicle_data, command and wake, so it reads only when the Owner
# slot cannot (slotFor("read")). Neither does I/O to answer ready or
# available. Both read the energy site's Wall Connector VINs for free, Owner
# first.
import time
from typing import Dict, Optional, Tuple

from ..Reconcile.Outcome import SATISFIED, SETTLED, UNSUPPORTED, Outcome

from .Capability import (
    CHARGE_FIELDS,
    CLOUD_FREE,
    COMMANDS,
    FLEET_COMMAND,
    FLEET_DATA,
    FLEET_WAKE,
    LOCATION_FIELDS,
    OWNER_COMMAND,
    READ_CHARGE,
    READ_WALL_CONNECTOR_VINS,
    SET_AMPS,
    SET_CHARGE_LIMIT,
    START_CHARGING,
    STOP_CHARGING,
    WAKE,
)
from .Provider import CLOUD_SUPPRESSED, Provider, carAmpsRange

# The Tesla command each capability sends, for the read-only line
TESLA_COMMAND = {
    SET_CHARGE_LIMIT: "set_charge_limit",
    START_CHARGING: "charge_start",
    STOP_CHARGING: "charge_stop",
    SET_AMPS: "set_charging_amps",
    WAKE: "wake_up",
}


class _TeslaAPIProvider(Provider):
    # One token slot of the TeslaAPI module
    FIELDS = CHARGE_FIELDS | LOCATION_FIELDS
    slotName = "ownerSlot"

    def __init__(self, carapi):
        self.carapi = carapi
        self.module = carapi

    def fetch(self, vehicle, field: str, allowWake: bool = False) -> bool:
        # Never wakes, whatever allowWake says: a cloud wake is never worth a
        # read. update_vehicle_data observes as the slot's provider
        car = vehicle.attachment("TeslaAPI")
        if car is None:
            return False
        if time.time() < car.statusDeferral:
            # As update_vehicle_data: a deferred car reports its stored
            # values without a request
            return True
        slot = getattr(self.carapi, self.slotName)
        # Free online check (vehicles/{vin}) before a paid vehicle_data
        return bool(
            self.carapi.is_awake(vehicle, slot=slot)
            and self.carapi.update_vehicle_data(vehicle, slot=slot)
        )

    def ampsRange(self, vehicle) -> Optional[Tuple[float, float]]:
        return carAmpsRange(vehicle, self.carapi.master)

    # Command methods: one request each (set_charging_amps below 5 A
    # excepted, as TeslaPy #42), classified by TeslaAPI.command. Each sends
    # with this provider's slot only, and only when canCommand lets it for
    # the car. No pre-wake: the attempt runner wakes first when the
    # attempt says so.

    # The clock passed to the wake hold; a test may replace it
    clock = time.time

    def _listed(self, vin: str):
        # The registry vehicle for vin when the account lists it, or None
        vehicle = self.carapi.registry.get(vin)
        return vehicle if vehicle and vehicle.attachment("TeslaAPI") else None

    def _command(self, vin: str, name: str, body: Optional[dict] = None) -> Outcome:
        vehicle = self._listed(vin)
        if vehicle is None:
            return Outcome(UNSUPPORTED, "not listed")
        return self.carapi.command(
            vehicle, name, body, slot=getattr(self.carapi, self.slotName)
        )

    def setChargeLimit(self, vin: str, limit: int) -> Outcome:
        return self._command(vin, "set_charge_limit", {"percent": int(limit)})

    def startCharging(self, vin: str) -> Outcome:
        outcome = self._command(vin, "charge_start")
        if outcome == Outcome(SATISFIED, "disconnected"):
            # Nothing to start: not a start
            return Outcome(UNSUPPORTED, "disconnected")
        return outcome

    def stopCharging(self, vin: str) -> Outcome:
        vehicle = self._listed(vin)
        if (
            vehicle is not None
            and self.carapi.canCommand(vehicle, getattr(self.carapi, self.slotName))
            and self.carapi.onlineState(vehicle, now=self.clock()) is False
        ):
            # An asleep car is not charging: settled, never sent
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
        vehicle = self._listed(vin)
        if vehicle is None:
            return Outcome(UNSUPPORTED, "not listed")
        return self.carapi.wakeOutcome(
            vehicle, now=self.clock(), slot=getattr(self.carapi, self.slotName)
        )


class OwnerAPIProvider(_TeslaAPIProvider):
    # Commands only a car Tesla lists with command_signing "off":
    # free, but paced as Fleet (OWNER_COMMAND)
    name = "OwnerAPI"
    rank = 4
    local = False
    # The site read is paced by its reader: a connect edge, then one poll a
    # minute
    DECLARATIONS = {
        READ_CHARGE: CLOUD_FREE,
        SET_CHARGE_LIMIT: OWNER_COMMAND,
        START_CHARGING: OWNER_COMMAND,
        STOP_CHARGING: OWNER_COMMAND,
        SET_AMPS: OWNER_COMMAND,
        WAKE: OWNER_COMMAND,
        READ_WALL_CONNECTOR_VINS: CLOUD_FREE,
    }
    SUPPRESSED = CLOUD_SUPPRESSED

    def ready(self, vehicle, capability) -> bool:
        if capability == READ_WALL_CONNECTOR_VINS:
            # A site read: no vehicle needed
            return self.carapi.wallConnectorVinsReady(self.carapi.ownerSlot)
        if vehicle is None or capability not in self.declarations():
            return False
        if vehicle.attachment("TeslaAPI") is None:
            return False
        if capability in COMMANDS:
            # Says once why a car is read-only
            if self.carapi.refuseCommandFor(vehicle, TESLA_COMMAND[capability]):
                return False
            return self.carapi.canCommand(vehicle, self.carapi.ownerSlot)
        return self.carapi.ownerSlot.usable("read")

    def available(self, capability) -> bool:
        # The broadcast gate for commands: an Owner token that may command
        # some car
        if capability in COMMANDS:
            return (
                self.carapi.ownerTokenHeld()
                and "command" not in self.carapi.ownerSlot.refusedPurposes
            )
        return True

    def wallConnectorVins(self) -> Optional[Dict[str, str]]:
        return self.carapi.wallConnectorVins(self.carapi.ownerSlot)


class FleetAPIProvider(_TeslaAPIProvider):
    name = "FleetAPI"
    slotName = "fleetSlot"
    rank = 5
    local = False
    DECLARATIONS = {
        READ_CHARGE: FLEET_DATA,
        SET_CHARGE_LIMIT: FLEET_COMMAND,
        START_CHARGING: FLEET_COMMAND,
        STOP_CHARGING: FLEET_COMMAND,
        SET_AMPS: FLEET_COMMAND,
        WAKE: FLEET_WAKE,
        READ_WALL_CONNECTOR_VINS: CLOUD_FREE,
    }
    SUPPRESSED = CLOUD_SUPPRESSED

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
        # the OwnerAPI provider)
        if capability in COMMANDS:
            usable = self.carapi.fleetSlot.usable("command")
            if not usable:
                self.carapi.noteReadOnly()
            return usable
        if capability == READ_WALL_CONNECTOR_VINS:
            return self.carapi.wallConnectorVinsReady(self.carapi.fleetSlot)
        return self.carapi.fleetSlot.usable("read")

    def wallConnectorVins(self) -> Optional[Dict[str, str]]:
        return self.carapi.wallConnectorVins(self.carapi.fleetSlot)
