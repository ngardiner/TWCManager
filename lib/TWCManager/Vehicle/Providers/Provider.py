from typing import Dict, FrozenSet, Optional, Tuple

from .Capability import Cost, Declaration


class Provider:
    name: str = ""
    # Fixed tie-break inside a cost tier: Gen2TWC 0, Gen3TWC 1, TeslaBLE 2,
    # telemetry 3, OwnerAPI 4, FleetAPI 5, HomeAssistant 6, anything else 100
    rank: int = 100
    local: bool = False
    # True when the provider's endpoints share one actuator, as Gen3 units on
    # one Neurio bus do
    sharedControl: bool = False
    # The legacy Vehicle module the facade calls; None for no module
    module = None
    # A ReadSource, or None
    readSource = None

    # The vin argument of the command methods is the vehicle's registry key: a
    # VIN, or an anon:... key that only local providers can serve.

    def declarations(self) -> Dict[str, Declaration]:
        return {}

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

    def covers(self, vehicle, amps) -> bool:
        span = self.ampsRange(vehicle)
        return span is not None and span[0] <= amps <= span[1]

    def setAmps(self, vin: str, amps: int) -> bool:
        raise NotImplementedError

    def wake(self, vin: str) -> bool:
        raise NotImplementedError

    def attempts(self) -> int:
        # Today's BLE 3 / API 2
        return 3 if self.local else 2

    def limitations(self, vehicle) -> FrozenSet[str]:
        return frozenset()

    def available(self, capability) -> bool:
        # A vehicle-less gate the order applies to every walk, so a broadcast
        # never reaches a provider that cannot command at all
        return True

    def summary(self, vehicle) -> dict:
        return {
            "name": self.name,
            "local": self.local,
            "capabilities": {
                capability: {
                    "tier": declaration.cost.tier,
                    "price": declaration.cost.price,
                    "ready": self.ready(vehicle, capability),
                }
                for capability, declaration in self.declarations().items()
            },
        }


def vehicleMaxAmps(vehicle, configConfig: dict) -> float:
    # Today's TeslaAPIEVSE.maxPower rule
    return vehicle.value("availableCurrent") or configConfig.get(
        "wiringMaxAmpsPerTWC", 32
    )
