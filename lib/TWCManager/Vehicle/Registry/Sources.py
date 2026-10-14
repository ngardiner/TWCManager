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
    }
)
LOCATION_FIELDS = frozenset({"lat", "lon", "gpsAsOf"})


class TeslaBLEReadSource(ReadSource):
    name = "TeslaBLE"
    order = 0
    fields = CHARGE_FIELDS | LOCATION_FIELDS

    def __init__(self, ble, master):
        self.ble = ble
        self.master = master

    def canRead(self, vehicle) -> bool:
        # Only claimed VINs: each miss costs the BLE command timeout.
        if not vehicle.vin or "TeslaBLE" not in vehicle.claims:
            return False
        vp = self.master.getModuleByName("VehiclePriority")
        return getattr(vp, "commandPolicy", "prefer_ble") != "api_only"

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
    name = "TeslaAPI"
    order = 1
    fields = CHARGE_FIELDS | LOCATION_FIELDS

    def __init__(self, carapi):
        self.carapi = carapi

    def canRead(self, vehicle) -> bool:
        return vehicle.attachment("TeslaAPI") is not None

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
        # Free online check (vehicles/{vin}) before a paid vehicle_data.
        if not car.is_awake():
            return None
        # update_vehicle_data observes through the CarApiVehicle fields.
        return {} if car.update_vehicle_data() else None


class HomeAssistantReadSource(ReadSource):
    name = "HomeAssistant"
    order = 1
    fields = CHARGE_FIELDS | LOCATION_FIELDS

    def __init__(self, ha):
        self.ha = ha

    def canRead(self, vehicle) -> bool:
        return vehicle.attachment("HomeAssistant") is not None

    def fetch(self, vehicle, field: str, allowWake: bool) -> Optional[Dict[str, Any]]:
        # Home Assistant never wakes the car. refresh_from_home_assistant
        # observes through the registry itself.
        hv = vehicle.attachment("HomeAssistant")
        if hv is None:
            return None
        hv.refresh_from_home_assistant(self.ha)
        return {}
