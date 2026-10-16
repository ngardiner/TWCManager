# The Gen3 TWC as a provider: the unit serving a vehicle sets its amps locally
# through the controller's shared fake-Neurio value. Every unit reads that one value,
# so a unit cannot be stopped alone while a sibling charges: declared as
# NO_INDIVIDUAL_STOP whenever more than one unit is occupied. The stop is a
# zero headroom, a normal local stop (unverified on hardware).
from typing import Optional, Tuple

from ..Reconcile.Outcome import DONE, UNSUPPORTED, Outcome
from .Capability import (
    LOCAL,
    NO_INDIVIDUAL_STOP,
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
    worker = "local"
    module = None
    DECLARATIONS = {
        SET_AMPS: Declaration(LOCAL),
        START_CHARGING: Declaration(LOCAL),
        STOP_CHARGING: Declaration(LOCAL),
    }
    SUPPRESSED = {}

    def __init__(self, controller, master):
        self.controller = controller
        self.master = master

    def unitFor(self, vehicle):
        return None if vehicle is None else self._unitForKey(vehicle.key)

    def _unitForKey(self, key):
        for unit in self.controller.devices():
            if unit.vehicleKey == key:
                return unit
        return None

    def ready(self, vehicle, capability) -> bool:
        return capability in self.DECLARATIONS and self.unitFor(vehicle) is not None

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

    def _onUnit(self, vin, act) -> Outcome:
        unit = self._unitForKey(vin)
        if unit is None:
            return Outcome(UNSUPPORTED, "no unit")
        act(unit)
        return Outcome(DONE)

    def setAmps(self, vin: str, amps: int) -> Outcome:
        return self._onUnit(vin, lambda u: u.setTargetPower(u.convertAmpsToWatts(amps)))

    def stopCharging(self, vin: str) -> Outcome:
        # A zero headroom: a normal local stop (unverified on hardware). The
        # shared value stops the unit only when no sibling holds a target
        return self._onUnit(vin, lambda u: u.controller.setUnitTarget(u.ID, 0))

    def startCharging(self, vin: str) -> Outcome:
        # The last target (or the minimum) back as the unit's own
        return self._onUnit(
            vin, lambda u: u.controller.setUnitTarget(u.ID, u.lastTarget or u.minPower)
        )
