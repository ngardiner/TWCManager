# The HomeAssistant module as a provider. HA's Tesla integration is commonly
# the user's own Fleet app, so its price is unknown: treated as billed and
# ranked after Fleet. It cannot wake the car.
from typing import Optional, Tuple

from .Capability import (
    HA_BILLED,
    READ_CHARGE,
    READ_LOCATION,
    SET_AMPS,
    SET_CHARGE_LIMIT,
    START_CHARGING,
    STOP_CHARGING,
    Declaration,
)
from .Provider import Provider, vehicleMaxAmps

CAPABILITIES = (
    READ_CHARGE,
    READ_LOCATION,
    SET_CHARGE_LIMIT,
    START_CHARGING,
    STOP_CHARGING,
    SET_AMPS,
)


class HomeAssistantProvider(Provider):
    name = "HomeAssistant"
    rank = 6
    local = False
    vehicleAttachment = "HomeAssistant"

    def __init__(self, ha):
        self.ha = ha
        self.module = ha

    @property
    def readSource(self):
        # Read through, so a rebuilt read source is never stale
        return self.ha.readSource

    def declarations(self):
        return {capability: Declaration(HA_BILLED) for capability in CAPABILITIES}

    def attempts(self) -> int:
        # HomeAssistant had no VehiclePriority priority: one call, as when it
        # was the command module itself
        return 1

    def available(self, capability) -> bool:
        # A disabled module gets no broadcast, as getModuleByName skipped it
        return bool(self.ha.enabled())

    def ready(self, vehicle, capability) -> bool:
        # Entity flags only: no I/O
        if vehicle is None or capability not in CAPABILITIES:
            return False
        if not self.ha.enabled():
            return False
        hv = vehicle.attachment("HomeAssistant")
        if hv is None:
            return False
        if capability == SET_AMPS:
            return bool(hv.has_charge_current_number)
        if capability in (START_CHARGING, STOP_CHARGING):
            return bool(hv.has_charge_switch)
        if capability == SET_CHARGE_LIMIT:
            return bool(hv.has_charge_limit_number)
        return True

    def ampsRange(self, vehicle) -> Optional[Tuple[float, float]]:
        if vehicle is None:
            return None
        configConfig = (getattr(self.ha.master, "config", None) or {}).get("config")
        return (1, vehicleMaxAmps(vehicle, configConfig or {}))

    def setAmps(self, vin: str, amps: int) -> bool:
        vehicle = self.ha.registry.get(vin)
        if vehicle is None:
            return False
        hv = vehicle.attachment("HomeAssistant")
        if hv is None:
            return False
        return bool(self.ha.setChargeRate(amps, vehicle=hv))
