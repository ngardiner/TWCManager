# The HomeAssistant module as a provider. HA's Tesla integration is commonly
# the user's own Fleet app, so its price is unknown: treated as billed and
# ranked after Fleet. It cannot wake the car.
from typing import Optional, Tuple

from ..Reconcile.Outcome import UNSUPPORTED, Outcome
from . import Capability as C
from .Provider import CLOUD_SUPPRESSED, Provider, carAmpsRange

# What Home Assistant's entities can report
HA_FIELDS = frozenset(
    {"batteryLevel", "chargeLimit", "availableCurrent", "chargingState", "lat", "lon"}
)


class HomeAssistantProvider(Provider):
    name = "HomeAssistant"
    rank = 6
    local = False
    DECLARATIONS = dict.fromkeys((C.COMMANDS - {C.WAKE}) | {C.READ_CHARGE}, C.HA_BILLED)
    SUPPRESSED = CLOUD_SUPPRESSED
    FIELDS = HA_FIELDS

    def __init__(self, ha):
        self.ha = ha
        self.module = ha

    def available(self, capability) -> bool:
        # A disabled module gets no broadcast, as getModuleByName skipped it
        return bool(self.ha.enabled())

    def ready(self, vehicle, capability) -> bool:
        # Entity flags only: no I/O
        if vehicle is None or not self.declares(capability):
            return False
        if not self.ha.enabled():
            return False
        hv = vehicle.attachment("HomeAssistant")
        if hv is None:
            return False
        if capability == C.SET_AMPS:
            return bool(hv.has_charge_current_number)
        if capability in (C.START_CHARGING, C.STOP_CHARGING):
            return bool(hv.has_charge_switch)
        if capability == C.SET_CHARGE_LIMIT:
            return bool(hv.has_charge_limit_number)
        return True

    def fetch(
        self,
        vehicle,
        field: str,
        allowWake: bool = False,
        cutoff: Optional[float] = None,
        wait: bool = False,
    ) -> bool:
        # Never wakes the car. None (unreachable, nothing answered) is a
        # failed read, never a stale value reported as one
        hv = vehicle.attachment("HomeAssistant")
        if hv is None:
            # Detached since ready()
            return False
        seen = hv.parse_from_home_assistant(self.ha)
        for k, v in (seen or {}).items():
            vehicle.observe(k, v, self.name)
        return field in (seen or {})

    def ampsRange(self, vehicle) -> Optional[Tuple[float, float]]:
        return carAmpsRange(vehicle, self.ha.master)

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
