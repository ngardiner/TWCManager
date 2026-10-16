# The HomeAssistant module as a provider. HA's Tesla integration is commonly
# the user's own Fleet app, so its price is unknown: treated as billed and
# ranked after Fleet. It cannot wake the car.
from typing import Optional, Tuple

from ..Reconcile.Outcome import UNSUPPORTED, Outcome
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
from .Provider import CLOUD_SUPPRESSED, Provider, vehicleMaxAmps

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
    DECLARATIONS = {
        READ_CHARGE: Declaration(HA_BILLED),
        READ_LOCATION: Declaration(HA_BILLED),
        SET_CHARGE_LIMIT: Declaration(HA_BILLED),
        START_CHARGING: Declaration(HA_BILLED),
        STOP_CHARGING: Declaration(HA_BILLED),
        SET_AMPS: Declaration(HA_BILLED),
    }
    SUPPRESSED = CLOUD_SUPPRESSED

    def __init__(self, ha):
        self.ha = ha
        self.module = ha

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

    # Command methods: one service call each through HomeAssistant's
    # _post_service, which classifies it

    def _entity(self, vin: str, kind: str):
        # (HaVehicle, entity ID of kind) for vin; either may be None
        vehicle = self.ha.registry.get(vin)
        hv = vehicle.attachment("HomeAssistant") if vehicle is not None else None
        if hv is None:
            return None, None
        return hv, (hv.entity_ids or {}).get(kind)

    def _call(self, vin: str, kind: str, domain: str, service: str, extra=None):
        hv, ent = self._entity(vin, kind)
        if hv is None:
            return Outcome(UNSUPPORTED, "not listed")
        if not ent:
            return Outcome(UNSUPPORTED, "no " + kind + " entity")
        data = {"entity_id": ent}
        data.update(extra or {})
        return self.ha._post_service(domain, service, data)

    def setChargeLimit(self, vin: str, limit: int) -> Outcome:
        return self._call(
            vin, "charge_limit_number", "number", "set_value", {"value": int(limit)}
        )

    def startCharging(self, vin: str) -> Outcome:
        return self._call(vin, "charge_switch", "switch", "turn_on")

    def stopCharging(self, vin: str) -> Outcome:
        return self._call(vin, "charge_switch", "switch", "turn_off")

    def setAmps(self, vin: str, amps: int) -> Outcome:
        # Clamped to 1..32 A (typical Tesla charger limits)
        value = min(32.0, max(1.0, float(amps)))
        return self._call(
            vin, "charge_current_number", "number", "set_value", {"value": value}
        )
