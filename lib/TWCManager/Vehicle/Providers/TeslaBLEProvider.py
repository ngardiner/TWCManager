# The TeslaBLE module as a provider: local and free, for every VIN the module
# holds a claim on. A paired key and an unproven one (seeded on the upgrade
# boot) are both tried; a refused key has dropped its claim, so it is not.
from typing import Optional, Tuple

from . import Capability as C
from ..Reconcile.Outcome import Outcome
from .Provider import Provider, carAmpsRange


class TeslaBLEProvider(Provider):
    name = "TeslaBLE"
    rank = 2
    local = True
    worker = "ble"
    DECLARATIONS = dict.fromkeys(C.COMMANDS | {C.READ_CHARGE}, C.LOCAL)
    # api_only blocks BLE commands and BLE reads alike
    SUPPRESSED = {"api_only": C.ALL}
    FIELDS = C.CHARGE_FIELDS | C.LOCATION_FIELDS

    def __init__(self, ble, master):
        self.ble = ble
        self.master = master
        self.module = ble

    def ready(self, vehicle, capability) -> bool:
        # Called from the distributor: reads the claim only, never settings
        # (settingsLock) or the adapter (_bleLock)
        if vehicle is None or not self.declares(capability):
            return False
        return bool(vehicle.vin) and "TeslaBLE" in vehicle.claims

    def ampsRange(self, vehicle) -> Optional[Tuple[float, float]]:
        # Never None for a ready vehicle: the order would read that as no
        # local path and let Fleet bill.
        return carAmpsRange(vehicle, self.master)

    def _get(self, vin: str, field: str) -> Optional[dict]:
        if field not in C.LOCATION_FIELDS:
            return self.ble.get_charge_state(vin)
        loc = self.ble.get_location_state(vin)
        if loc is None:
            return None
        # Only registry field names: heading is not a field. The fix time
        # first: the position's observers date the fix by it
        return {
            "gpsAsOf": loc.get("gpsAsOf") or None,
            "lat": loc.get("latitude"),
            "lon": loc.get("longitude"),
        }

    def fetch(
        self,
        vehicle,
        field: str,
        allowWake: bool = False,
        cutoff: Optional[float] = None,
        wait: bool = False,
    ) -> bool:
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
