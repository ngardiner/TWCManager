# Read sources for the Tesla providers: BLE (local, free) first, then the
# cloud API. Neither wakes the car to read unless the caller allows it, and
# the cloud source never wakes at all (a Fleet wake costs 10x a data read).
import time
from typing import Any, Dict, Optional

from .ReadSource import ReadSource

CHARGE_FIELDS = frozenset(
    {
        "batteryLevel",
        "chargeLimit",
        "chargingState",
        "availableCurrent",
        "actualCurrent",
        "voltage",
        "phases",
        "scheduledChargingPending",
        "timeToFullCharge",
        "chargeCurrentRequestMax",
    }
)
LOCATION_FIELDS = frozenset({"lat", "lon", "gpsAsOf"})


class TeslaBLEReadSource(ReadSource):
    name = "TeslaBLE"
    order = 0
    fields = CHARGE_FIELDS | LOCATION_FIELDS
    provider = "TeslaBLE"

    def __init__(self, ble, master):
        self.ble = ble
        self.master = master

    def canRead(self, vehicle) -> bool:
        # Only claimed VINs: each miss costs the BLE command timeout.
        # commandPolicy is not consulted here: the provider order drops a
        # suppressed source before the cascade sees it.
        return bool(vehicle.vin) and "TeslaBLE" in vehicle.claims

    def _location(self, vin: str) -> Optional[Dict[str, Any]]:
        loc = self.ble.get_location_state(vin)
        if loc is None:
            return None
        # Only registry field names: heading is not a field.
        result = {"lat": loc.get("latitude"), "lon": loc.get("longitude")}
        if loc.get("gpsAsOf"):
            result["gpsAsOf"] = loc["gpsAsOf"]
        return result

    def _get(self, vin: str, field: str) -> Optional[Dict[str, Any]]:
        if field in LOCATION_FIELDS:
            return self._location(vin)
        return self.ble.get_charge_state(vin)

    def fetch(self, vehicle, field: str, allowWake: bool) -> Optional[Dict[str, Any]]:
        vin = vehicle.vin
        result = self._get(vin, field)
        if result is None and allowWake and self.ble.wakeVehicle(vin):
            result = self._get(vin, field)
        if result is None:
            return None
        # A None is "not reported", not a reading. A reply with nothing left
        # is a failed read: {} would tell the cascade the source recorded
        # its observations itself and stop it short of the cloud.
        filtered = {
            k: v for k, v in result.items() if v is not None and k in self.fields
        }
        return filtered or None


class TeslaAPIReadSource(ReadSource):
    # One per token slot. The observation source stays "TeslaAPI"; the
    # provider says which slot paid for the read.
    name = "TeslaAPI"
    order = 1
    fields = CHARGE_FIELDS | LOCATION_FIELDS

    def __init__(self, carapi, kind: str = "owner"):
        self.carapi = carapi
        self.kind = kind
        self.provider = "FleetAPI" if kind == "fleet" else "OwnerAPI"

    def slot(self):
        if self.kind == "fleet":
            return self.carapi.fleetSlot
        return self.carapi.ownerSlot

    def canRead(self, vehicle) -> bool:
        if vehicle.attachment("TeslaAPI") is None:
            return False
        if not self.slot().usable("read"):
            return False
        # A Fleet vehicle_data is paid: only when the Owner slot cannot read,
        # as slotFor("read") chooses
        return self.kind != "fleet" or not self.carapi.ownerSlot.usable("read")

    def fetch(self, vehicle, field: str, allowWake: bool) -> Optional[Dict[str, Any]]:
        # Never wakes, whatever allowWake says: a cloud wake is never worth a
        # read.
        car = vehicle.attachment("TeslaAPI")
        if car is None:
            return None
        if time.time() < car.statusDeferral:
            # As update_vehicle_data: a deferred car reports its stored
            # values without a request.
            return {}
        slot = self.slot()
        # Free online check (vehicles/{vin}) before a paid vehicle_data.
        if not car.is_awake(slot=slot):
            return None
        # update_vehicle_data observes through the CarApiVehicle fields.
        return {} if car.update_vehicle_data(slot=slot) else None


# What Home Assistant's entities can report.
HA_FIELDS = frozenset(
    {"batteryLevel", "chargeLimit", "availableCurrent", "chargingState", "lat", "lon"}
)


class HomeAssistantReadSource(ReadSource):
    name = "HomeAssistant"
    # After the cloud API (1): HA mirrors the cloud, so it reads last.
    order = 2
    fields = HA_FIELDS
    provider = "HomeAssistant"

    def __init__(self, ha):
        self.ha = ha

    def canRead(self, vehicle) -> bool:
        return vehicle.attachment("HomeAssistant") is not None

    def fetch(self, vehicle, field: str, allowWake: bool) -> Optional[Dict[str, Any]]:
        # Home Assistant never wakes the car. The cascade records what comes
        # back; None (unreachable, nothing answered) lets it fail rather than
        # report a stale value as a read.
        hv = vehicle.attachment("HomeAssistant")
        if hv is None:
            return None
        seen = hv.parse_from_home_assistant(self.ha)
        return seen or None
