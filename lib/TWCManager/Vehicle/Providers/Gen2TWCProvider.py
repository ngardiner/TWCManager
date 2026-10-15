# The Gen2 TWC as a provider: the slave serving a vehicle reads its plug
# state, VIN and amps passively and sets its amps locally. Its stop is
# disruptive (it stops answering every slave, or stops the TWC itself), so it is
# declared at the very-high-cost tier and only chargeStopMode selects it.
# Command methods never touch RS-485: a stop is published for the serial
# thread's heartbeat (slave.stopRequested), a target likewise.
from typing import Optional, Tuple

from .Capability import (
    DISRUPTIVE,
    LOCAL,
    READ_AMPS,
    READ_PLUG,
    READ_VIN,
    SET_AMPS,
    START_CHARGING,
    STOP_CHARGING,
    Declaration,
)
from ..Reconcile.Outcome import DONE, UNSUPPORTED, Outcome
from ..Registry.VIN import isWellFormedVIN
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
            START_CHARGING: Declaration(LOCAL),
            STOP_CHARGING: Declaration(DISRUPTIVE),
        }

    def _settings(self) -> dict:
        return getattr(self.master, "settings", None) or {}

    def slaveFor(self, vehicle):
        return None if vehicle is None else self._slaveForKey(vehicle.key)

    def _slaveForKey(self, key):
        if isinstance(key, str) and isWellFormedVIN(key):
            # vehicleKey is the upper-case VIN; an anon: key is kept as is
            key = key.upper()
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
        if capability == START_CHARGING:
            # Mode 2's start is to answer slaves again. It undoes only a stop
            # this provider delivered, never a BLE or cloud stop
            if self.stopMode() != 2 or self.slaveFor(vehicle) is None:
                return False
            desired = getattr(self.master, "desired", None)
            if desired is None:
                return False
            return desired.state(vehicle.key).lastStopProvider == self.name
        return False

    def ampsRange(self, vehicle) -> Optional[Tuple[float, float]]:
        slave = self.slaveFor(vehicle)
        if slave is None:
            return None
        if self.chargeRateControl() == 3:
            # Mode 3 hands the rate to the vehicle below 6 A
            # (TWCSlave.send_master_heartbeat), and the TWC never offers
            # below its own minimum; below the floor the next provider
            # carries the target
            return (max(6, slave.minAmpsToOffer()), slave.maxOfferAmps)
        return (slave.minAmps, slave.maxOfferAmps)

    def setAmps(self, vin: str, amps: int) -> Outcome:
        # Publishes a target the serial loop sends on the next heartbeat
        slave = self._slaveForKey(vin)
        if slave is None:
            return Outcome(UNSUPPORTED, "no slave")
        slave.setTargetPower(slave.convertAmpsToWatts(amps))
        return Outcome(DONE)

    def startCharging(self, vin: str) -> Outcome:
        # Answer slaves again (the heartbeat reads the setting)
        with self.master.settingsLock:
            self.master.settings["respondToSlaves"] = 1
        return Outcome(DONE, "respond")

    def stopCharging(self, vin: str) -> Outcome:
        slave = self._slaveForKey(vin)
        if slave is None:
            return Outcome(UNSUPPORTED, "no slave")
        mode = self.stopMode()
        if mode == 2:
            self.master.stopRespondingToSlaves()
            return Outcome(DONE, "respond")
        if mode == 3:
            # Sent by this slave's next heartbeat, on the serial thread
            slave.stopRequested = True
            return Outcome(DONE, "queued")
        return Outcome(UNSUPPORTED, "mode %d" % mode)
