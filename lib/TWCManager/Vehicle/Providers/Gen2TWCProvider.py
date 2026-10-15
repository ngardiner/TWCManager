# The Gen2 TWC as a provider: the slave serving a vehicle reads its plug
# state, VIN and amps passively and sets its amps locally. Its stop is
# disruptive (it stops answering every slave, or stops the TWC itself), so it is
# declared at the very-high-cost tier and only chargeStopMode selects it.
from typing import Optional, Tuple

from .Capability import (
    DISRUPTIVE,
    LOCAL,
    READ_AMPS,
    READ_PLUG,
    READ_VIN,
    SET_AMPS,
    STOP_CHARGING,
    Declaration,
)
from .Provider import Provider

READ_CAPABILITIES = (READ_PLUG, READ_VIN, READ_AMPS)


class Gen2TWCProvider(Provider):
    name = "Gen2TWC"
    rank = 0
    local = True
    module = None

    def __init__(self, master):
        self.master = master

    def declarations(self):
        return {
            READ_PLUG: Declaration(LOCAL, passive=True),
            READ_VIN: Declaration(LOCAL, passive=True),
            READ_AMPS: Declaration(LOCAL, passive=True),
            SET_AMPS: Declaration(LOCAL),
            STOP_CHARGING: Declaration(DISRUPTIVE),
        }

    def _settings(self) -> dict:
        return getattr(self.master, "settings", None) or {}

    def slaveFor(self, vehicle):
        return None if vehicle is None else self._slaveForKey(vehicle.key)

    def _slaveForKey(self, key):
        for slave in self.master.getSlaveTWCs():
            if getattr(slave, "vehicleKey", None) == key:
                return slave
        return None

    def stopMode(self) -> int:
        return int(self._settings().get("chargeStopMode", 1))

    def chargeRateControl(self) -> int:
        return int(self._settings().get("chargeRateControl", 1))

    def ready(self, vehicle, capability) -> bool:
        if capability in READ_CAPABILITIES:
            return self.slaveFor(vehicle) is not None
        if capability == SET_AMPS:
            # Mode 2 hands the rate to the vehicle
            return self.chargeRateControl() != 2 and self.slaveFor(vehicle) is not None
        if capability == STOP_CHARGING:
            # Off by default: only chargeStopMode selects a disruptive stop
            return self.stopMode() in (2, 3) and self.slaveFor(vehicle) is not None
        return False

    def ampsRange(self, vehicle) -> Optional[Tuple[float, float]]:
        slave = self.slaveFor(vehicle)
        if slave is None:
            return None
        if self.chargeRateControl() == 3:
            # Mode 3 hands the rate to the vehicle below the TWC's own
            # minimum offer; below it the next provider carries the target
            return (slave.minAmpsToOffer(), slave.maxOfferAmps)
        return (slave.minAmps, slave.maxOfferAmps)

    def setAmps(self, vin: str, amps: int) -> bool:
        # Publishes a target the serial loop sends on the next heartbeat
        slave = self._slaveForKey(vin)
        if slave is None:
            return False
        slave.setTargetPower(slave.convertAmpsToWatts(amps))
        return True

    def stopCharging(self, vin: str) -> bool:
        # No Phase 2 caller: TWCMaster.stopCarsCharging is unchanged
        mode = self.stopMode()
        if mode == 2:
            self.master.stopRespondingToSlaves()
            return True
        if mode == 3:
            slave = self._slaveForKey(vin)
            if slave is None:
                return False
            self.master.sendStopCommand(slave.TWCID)
            return True
        return False
