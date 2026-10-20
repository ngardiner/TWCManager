# The TeslaBLE module as a provider: local and free, for every VIN the module
# holds a claim on. A paired key and an unproven one (seeded on the upgrade
# boot) are both tried; a refused key has dropped its claim, so it is not.
from typing import Optional, Tuple

from .Capability import (
    ALL,
    CHARGE_FIELDS,
    LOCAL,
    LOCATION_FIELDS,
    READ_CHARGE,
    SET_AMPS,
    SET_CHARGE_LIMIT,
    START_CHARGING,
    STOP_CHARGING,
    WAKE,
    Declaration,
)
from ..Reconcile.Outcome import Outcome
from .Provider import Provider, vehicleMaxAmps

CAPABILITIES = (
    READ_CHARGE,
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
    worker = "ble"
    DECLARATIONS = {capability: Declaration(LOCAL) for capability in CAPABILITIES}
    # api_only blocks BLE commands and BLE reads alike
    SUPPRESSED = {"api_only": ALL}
    FIELDS = CHARGE_FIELDS | LOCATION_FIELDS

    def __init__(self, ble, master):
        self.ble = ble
        self.master = master
        self.module = ble

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

    def _get(self, vin: str, field: str) -> Optional[dict]:
        if field not in LOCATION_FIELDS:
            return self.ble.get_charge_state(vin)
        loc = self.ble.get_location_state(vin)
        if loc is None:
            return None
        # Only registry field names: heading is not a field
        return {
            "lat": loc.get("latitude"),
            "lon": loc.get("longitude"),
            "gpsAsOf": loc.get("gpsAsOf") or None,
        }

    def fetch(self, vehicle, field: str, allowWake: bool = False) -> bool:
        # Only for a ready (claimed) VIN: each miss costs the command timeout
        vin = vehicle.vin
        result = self._get(vin, field)
        if result is None and allowWake and self.ble.wakeVehicle(vin):
            result = self._get(vin, field)
        # A None is "not reported", not a reading
        for k, v in (result or {}).items():
            if v is not None and k in self.FIELDS:
                vehicle.observe(k, v, self.name)
        return (result or {}).get(field) is not None

    # Command methods: one tesla-control command each, classified by
    # TeslaBLE.commandOutcome. No pre-wake: the worker wakes first when the
    # attempt says so.

    def setChargeLimit(self, vin: str, limit: int) -> Outcome:
        return self.ble.commandOutcome(vin, "charging-set-limit", int(limit))

    def startCharging(self, vin: str) -> Outcome:
        return self.ble.commandOutcome(vin, "charging-start")

    def stopCharging(self, vin: str) -> Outcome:
        return self.ble.commandOutcome(vin, "charging-stop")

    def setAmps(self, vin: str, amps: int) -> Outcome:
        return self.ble.commandOutcome(vin, "charging-set-amps", int(amps))

    def wake(self, vin: str) -> Outcome:
        return self.ble.commandOutcome(vin, "wake")
