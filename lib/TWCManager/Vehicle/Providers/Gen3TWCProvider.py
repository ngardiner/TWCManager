# The Gen3 TWC as a provider: the unit serving a vehicle reads its plug state
# and amps passively from the cached vitals and sets its amps locally through
# the controller's shared fake-Neurio value. Every unit reads that one value,
# so a unit cannot be stopped alone while a sibling charges: declared as
# NO_INDIVIDUAL_STOP whenever more than one unit is occupied. The stop is a
# zero headroom, a normal local stop (unverified on hardware).
from typing import Optional, Tuple

from .Capability import (
    LOCAL,
    NO_INDIVIDUAL_STOP,
    READ_AMPS,
    READ_PLUG,
    SET_AMPS,
    START_CHARGING,
    STOP_CHARGING,
    Declaration,
)
from .Provider import Provider


class Gen3TWCProvider(Provider):
    name = "Gen3TWC"
    rank = 1
    local = True
    module = None
    sharedControl = True

    def __init__(self, controller, master):
        self.controller = controller
        self.master = master

    def declarations(self):
        return {
            READ_PLUG: Declaration(LOCAL, passive=True),
            READ_AMPS: Declaration(LOCAL, passive=True),
            SET_AMPS: Declaration(LOCAL),
            START_CHARGING: Declaration(LOCAL),
            STOP_CHARGING: Declaration(LOCAL),
        }

    def unitFor(self, vehicle):
        return None if vehicle is None else self._unitForKey(vehicle.key)

    def _unitForKey(self, key):
        for unit in self.controller.devices():
            if unit.vehicleKey == key:
                return unit
        return None

    def ready(self, vehicle, capability) -> bool:
        return capability in self.declarations() and self.unitFor(vehicle) is not None

    def ampsRange(self, vehicle) -> Optional[Tuple[float, float]]:
        unit = self.unitFor(vehicle)
        if unit is None:
            return None
        return (6, unit.fuseAmps)

    def limitations(self, vehicle):
        occupied = [u for u in self.controller.devices() if u.occupied is True]
        if len(occupied) > 1:
            return frozenset({NO_INDIVIDUAL_STOP})
        return frozenset()

    def setAmps(self, vin: str, amps: int) -> bool:
        unit = self._unitForKey(vin)
        if unit is None:
            return False
        unit.setTargetPower(unit.convertAmpsToWatts(amps))
        return True

    def stopCharging(self, vin: str) -> bool:
        # No Phase 2 caller: TWCMaster.stopCarsCharging is unchanged
        unit = self._unitForKey(vin)
        if unit is None:
            return False
        unit.stopCharging()
        return True

    def startCharging(self, vin: str) -> bool:
        # No Phase 2 caller
        unit = self._unitForKey(vin)
        if unit is None:
            return False
        unit.startCharging()
        return True
