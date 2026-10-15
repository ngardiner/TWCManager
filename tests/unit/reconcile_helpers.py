# Shared fixtures for the Reconcile tests: a settable clock, a provider whose
# command methods return scripted outcomes, and a provider order on a Mock
# master.
import time
from typing import List
from unittest.mock import Mock

from TWCManager.Vehicle.Providers import Capability as C
from TWCManager.Vehicle.Providers.Order import ProviderOrder
from TWCManager.Vehicle.Providers.Provider import Provider
from TWCManager.Vehicle.Reconcile.Outcome import ERROR, Outcome
from TWCManager.Vehicle.Registry.VehicleRegistry import VehicleRegistry

# Every command at the local tier; pass tier= to declare them at another cost
COMMAND_CAPS = {capability: C.Declaration(C.LOCAL) for capability in C.COMMANDS}


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class ScriptedProvider(Provider):
    # The registry's clock domain, as Task 5's Fleet provider
    clock = time.time

    def __init__(
        self,
        name,
        rank,
        local,
        outcomes: List[Outcome],
        caps=COMMAND_CAPS,
        amps=(1, 32),
        tier=None,
        module=object(),
        readyFor=None,
    ):
        self.name, self.rank, self.local = name, rank, local
        if tier is not None:
            caps = {
                capability: declaration._replace(cost=tier)
                for capability, declaration in caps.items()
            }
        self._caps = dict(caps)
        self._amps = amps
        self.module = module
        self.readyFor = readyFor
        # Public: a test may replace the script
        self.outcomes = list(outcomes)
        self.calls = []

    def declarations(self):
        return self._caps

    def ready(self, vehicle, capability) -> bool:
        if not (vehicle and vehicle.vin):
            return False
        return self.readyFor is None or bool(self.readyFor(vehicle, capability))

    def ampsRange(self, vehicle):
        return self._amps

    def _next(self, method, vin, value) -> Outcome:
        self.calls.append((method, vin, value))
        if not self.outcomes:
            return Outcome(ERROR, "unscripted")
        # The last outcome repeats
        return self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]

    def setChargeLimit(self, vin, limit):
        return self._next("setChargeLimit", vin, limit)

    def startCharging(self, vin):
        return self._next("startCharging", vin, None)

    def stopCharging(self, vin):
        return self._next("stopCharging", vin, None)

    def setAmps(self, vin, amps):
        return self._next("setAmps", vin, amps)

    def wake(self, vin):
        return self._next("wake", vin, None)

    def probe(self, vin):
        return self._next("probe", vin, None)


def make_order(settings=None, *providers):
    # As test_provider_order's master() and order()
    m = Mock()
    m.settings = settings or {}
    m.config = {}
    m.stats = {"moduleSuccess": {}, "moduleFailures": {}, "moduleDispatch": {}}
    m.vehicleRegistry = VehicleRegistry({})
    # A lane without a reconciler takes its pass-through path; a plain Mock
    # would answer beforeWrite with a Mock
    del m.reconciler
    o = ProviderOrder(m)
    for p in providers:
        o.register(p)
    return m, o
