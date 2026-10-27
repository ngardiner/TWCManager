# Shared fixtures for the Reconcile tests: a settable clock, a provider whose
# command methods return scripted outcomes, and a provider order on a Mock
# master.
import time
from types import SimpleNamespace
from typing import List
from unittest.mock import Mock

from TWCManager.Vehicle.Providers import Capability as C
from TWCManager.Vehicle.Providers.Order import ProviderOrder
from TWCManager.Vehicle.Providers.Provider import Provider
from TWCManager.Vehicle.Reconcile.Outcome import ERROR, Outcome
from TWCManager.Vehicle.Reconcile.Run import Job
from TWCManager.Vehicle.Registry.VehicleRegistry import VehicleRegistry
from TWCManager.VehicleCommandQueue import VehicleCommandQueue


def settled(store, key, aspect) -> bool:
    # The desire's attempt record settled at or after the desire
    state = store.peek(key)
    desired = state and state.desired.get(aspect)
    record = state and state.records.get(aspect)
    return bool(
        desired
        and record
        and record.settledAt is not None
        and record.settledAt >= desired.at
    )


# Every command at the local tier; pass tier= to declare them at another cost
COMMAND_CAPS = {capability: C.LOCAL for capability in C.COMMANDS}


# Presence from events, as live: home is set and TeslaMate's free
# position lands in the registry
HOME, AWAY = (40.0, -75.0), (40.922592, -76.606221)


def placeAt(master, vehicle, at=HOME):
    master.settings.update(homeLat=HOME[0], homeLon=HOME[1])
    vehicle.observe("lat", at[0], "TeslaMateVehicle")
    vehicle.observe("lon", at[1], "TeslaMateVehicle")


def bleAnswers(master, ok=True):
    # A scripted TeslaBLE's ping: an answer is contact and no answer is
    # absence, as TeslaBLE.ping observes them
    def ping(vin):
        master.vehicleRegistry.get(vin).observe("bleReach", ok, "TeslaBLE", seen=ok)
        return ok

    master.providerOrder.provider("TeslaBLE").module = SimpleNamespace(ping=ping)


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
        # A radio whose sleep-safe ping answers, with no sleep status
        module=SimpleNamespace(ping=lambda vin: True),
        readyFor=None,
        worker=None,
    ):
        self.name, self.rank, self.local = name, rank, local
        # As the real providers: the TWCs run inline, BLE on its own worker
        self.worker = worker or {
            "Gen2TWC": "local",
            "Gen3TWC": "local",
            "TeslaBLE": "ble",
        }.get(name, "cloud")
        if tier is not None:
            caps = dict.fromkeys(caps, tier)
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

    def reads(self, value, field="chargeLimit", cost=None):
        # Makes the provider fetch field as value (READ_CHARGE, at its own
        # locality's free tier unless cost says otherwise), counting fetches
        cost = cost or (C.LOCAL if self.local else C.CLOUD_FREE)
        self._caps = dict(self._caps, **{C.READ_CHARGE: cost})
        self.FIELDS, self.readValue, self.fetched = frozenset({field}), value, 0
        return self

    def fetch(self, vehicle, field, allowWake=False):
        self.fetched += 1
        vehicle.observe(field, self.readValue, self.name)
        return True

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


def make_order(settings=None, *providers):
    # As test_provider_order's master() and order()
    m = Mock()
    m.settings = settings or {}
    m.config = {}
    m.vehicleRegistry = VehicleRegistry({})
    # No reconciler until a test makes one: a plain Mock would take the debug
    # page's one-shots
    del m.reconciler
    o = m.vehicleRegistry.providerOrder = ProviderOrder(m)
    for p in providers:
        o.register(p)
    return m, o


class InlineQueue(VehicleCommandQueue):
    # Test-only: runs each attempt, plug check and online check at once on
    # the caller's thread, reporting it as a started queue would. Closed, it
    # takes stops only. Another job waits until the test runs it
    # (runPending), as a busy worker would leave it
    def start(self):
        pass

    def put(self, entry) -> bool:
        if isinstance(entry, Job) and entry.kind not in ("plugCheck", "onlineCheck"):
            return super().put(entry)
        if self._draining and not self._isHigh(entry):
            self._lose([(entry, "dropped")])
            return False
        with self._cond:
            self._busy = True
        self._runOne(entry)
        return True

    def waitDrained(self, deadline) -> bool:
        return True


def inline(reconciler):
    # Swaps the reconciler's two queues for InlineQueues with the same wiring
    reconciler.ble, reconciler.cloud = (
        InlineQueue(
            name=q.name,
            run=q._runEntry,
            key=q._keyOf,
            isHigh=q._isHigh,
            report=q._report,
            lost=q._lost,
        )
        for q in (reconciler.ble, reconciler.cloud)
    )
    return reconciler


def runPending(q):
    # A worker's loop on the caller's thread, until nothing is pending
    while entries(q):
        q._runOne(q._next())


def entries(q):
    # A queue's pending attempts, stops first
    with q._cond:
        return list(q._high.values()) + list(q._low.values())
