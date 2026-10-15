# The TeslaBLE module as a provider: local and free, for every VIN the module
# holds a claim on. A paired key and an unproven one (seeded on the upgrade
# boot) are both tried; a refused key has dropped its claim, so it is not.
from typing import Optional, Tuple

from .Capability import (
    LOCAL,
    READ_CHARGE,
    READ_LOCATION,
    READ_ONLINE,
    READ_PLUG,
    SET_AMPS,
    SET_CHARGE_LIMIT,
    START_CHARGING,
    STOP_CHARGING,
    WAKE,
    Declaration,
)
from .Provider import Provider, vehicleMaxAmps

CAPABILITIES = (
    READ_CHARGE,
    READ_LOCATION,
    READ_PLUG,
    READ_ONLINE,
    SET_CHARGE_LIMIT,
    START_CHARGING,
    STOP_CHARGING,
    SET_AMPS,
    WAKE,
)


class TeslaBLEProvider(Provider):
    name = "TeslaBLE"
    rank = 2
    local = True

    def __init__(self, ble, master):
        self.ble = ble
        self.master = master
        self.module = ble
        self.readSource = ble.readSource

    def declarations(self):
        return {capability: Declaration(LOCAL) for capability in CAPABILITIES}

    def ready(self, vehicle, capability) -> bool:
        # Called from the distributor: reads the claim only, never settings
        # (settingsLock) or the adapter (_bleLock)
        if vehicle is None or capability not in CAPABILITIES:
            return False
        return bool(vehicle.vin) and "TeslaBLE" in vehicle.claims

    def ampsRange(self, vehicle) -> Optional[Tuple[float, float]]:
        # Vehicle amps run 1..max, never 0. Never None for a ready vehicle:
        # the order would read that as no local path and let Fleet bill.
        if vehicle is None:
            return None
        configConfig = (getattr(self.master, "config", None) or {}).get("config")
        return (1, vehicleMaxAmps(vehicle, configConfig or {}))

    def setAmps(self, vin: str, amps: int) -> bool:
        return bool(self.ble.setChargeRate(int(amps), vin))

    def wake(self, vin: str) -> bool:
        return bool(self.ble.wakeVehicle(vin))
