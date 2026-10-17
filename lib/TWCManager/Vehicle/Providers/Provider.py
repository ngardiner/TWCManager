from typing import Dict, FrozenSet, Optional, Tuple

from ..Reconcile.Outcome import UNSUPPORTED, Outcome
from .Capability import COMMANDS, START_CHARGING, STOP_CHARGING, Cost, Declaration

# What a cloud provider may not do under each suppression condition: no
# command under ble_only, no stop when the user chose a disruptive local
# stop (chargeStopMode 2/3), which pays nothing, and under mode 2 no start
# either: on main a mode-2 start only answered the slaves again
CLOUD_SUPPRESSED = {
    "ble_only": COMMANDS,
    "localStops": frozenset({STOP_CHARGING}),
    "silentStops": frozenset({START_CHARGING}),
}


class Provider:
    name: str = ""
    # Fixed tie-break inside a cost tier: Gen2TWC 0, Gen3TWC 1, TeslaBLE 2,
    # OwnerAPI 4, FleetAPI 5, HomeAssistant 6, anything else 100
    rank: int = 100
    local: bool = False
    # The attempt worker its commands run on: "ble", "cloud", or "local"
    # (no I/O, run inline on the caller's thread)
    worker: str = "cloud"
    # The Vehicle module behind the provider; None for none
    module = None

    # What the provider can do, at what cost and how often
    DECLARATIONS: Dict[str, Declaration] = {}
    # Suppression condition ("ble_only", "api_only", "localStops") to the
    # capabilities it suppresses here; None for the locality rule
    SUPPRESSED: Optional[Dict[str, FrozenSet[str]]] = None
    # The registry fields fetch can read; empty for a provider that reads
    # nothing on demand
    FIELDS: FrozenSet[str] = frozenset()

    # The vin argument of the command methods is the vehicle's registry key: a
    # VIN, or an anon:... key that only local providers can serve.

    def declarations(self) -> Dict[str, Declaration]:
        return self.DECLARATIONS

    def declares(self, capability) -> bool:
        return capability in self.declarations()

    def cost(self, capability) -> Optional[Cost]:
        declaration = self.declarations().get(capability)
        return declaration.cost if declaration else None

    def ready(self, vehicle, capability) -> bool:
        # Per vehicle; never does I/O
        return False

    def ampsRange(self, vehicle) -> Optional[Tuple[float, float]]:
        return None

    def fetch(self, vehicle, field: str, allowWake: bool = False) -> bool:
        # Reads field (and whatever comes with it) into the registry; True
        # when the provider now vouches for vehicle.latest(field). Does I/O:
        # called with no lock held. Never wakes the car unless allowWake.
        return False

    def covers(self, vehicle, amps) -> bool:
        span = self.ampsRange(vehicle)
        return span is not None and span[0] <= amps <= span[1]

    # Command methods: each returns an Outcome (Reconcile/Outcome.py's
    # contract). Only a worker calls them; a provider never stamps a write.

    def setChargeLimit(self, vin: str, limit: int) -> Outcome:
        return Outcome(UNSUPPORTED, "not implemented")

    def startCharging(self, vin: str) -> Outcome:
        return Outcome(UNSUPPORTED, "not implemented")

    def stopCharging(self, vin: str) -> Outcome:
        return Outcome(UNSUPPORTED, "not implemented")

    def setAmps(self, vin: str, amps: int) -> Outcome:
        return Outcome(UNSUPPORTED, "not implemented")

    def wake(self, vin: str) -> Outcome:
        # A billed wake that fails holds the next one itself, a free online
        # check until then: the reconciler's follow-up wakes again (WAKE_CHECK)
        return Outcome(UNSUPPORTED, "not implemented")

    def limitations(self, vehicle) -> FrozenSet[str]:
        return frozenset()

    def available(self, capability) -> bool:
        # A vehicle-less gate the order applies to every walk, so a broadcast
        # never reaches a provider that cannot command at all
        return True


def suppressedFor(provider, condition) -> FrozenSet[str]:
    # A provider without a table (a test double, a Mock) gets the locality
    # rule; anything not known to be local is cloud, so it fails closed
    table = getattr(provider, "SUPPRESSED", None)
    if not isinstance(table, dict):
        table = {} if getattr(provider, "local", False) is True else CLOUD_SUPPRESSED
    return table.get(condition, frozenset())


def vehicleMaxAmps(vehicle, configConfig: dict) -> float:
    # VehicleEndpoint.maxPower's rule
    return vehicle.value("availableCurrent") or configConfig.get(
        "wiringMaxAmpsPerTWC", 32
    )
