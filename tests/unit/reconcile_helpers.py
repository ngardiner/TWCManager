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
from TWCManager.Vehicle.TeslaBLE import TeslaBLE
from TWCManager.VehicleCommandQueue import VehicleCommandQueue
from tests.unit.teslatokens import fleet_token, new_api, owner_token


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


def _ble(master):
    return master.providerOrder.provider("TeslaBLE").module


def _stub(master, name, fn):
    # A real TeslaBLE gets the method replaced; a scripted provider's shared
    # default module is never mutated, so it gets a fresh one
    ble = _ble(master)
    if isinstance(ble, TeslaBLE):
        setattr(ble, name, fn)
    else:
        master.providerOrder.provider("TeslaBLE").module = SimpleNamespace(**{name: fn})


def bleAnswers(master, asleep=None, answer=True, charging=None, ok=None):
    # A scripted TeslaBLE's ping: an answer is contact and no answer is
    # absence, as TeslaBLE.ping observes them; an answer gives the sleep
    # state and, awake, the door (charging: the cable holds it open)
    answer = answer if ok is None else ok
    calls = SimpleNamespace(count=0)

    def ping(vin, scan=True):
        calls.count += 1
        v = master.vehicleRegistry.get(vin)
        if answer is not None:
            v.observe("bleReach", answer, "TeslaBLE", seen=answer)
        if answer is True and ok is None:
            v.observe("state", "asleep" if asleep else "online", "TeslaBLE")
            if charging is not None and not asleep:
                v.observe("chargePortOpen", bool(charging), "TeslaBLE")
        return answer

    _stub(master, "ping", ping)
    return calls


def bleReads(master, soc=50, charging="Stopped", amps=0, limit=80):
    # A scripted BLE charge read, counted; the test may change its answers.
    # Every car holds a key: only a claimed car is read over BLE
    reads = SimpleNamespace(count=0, soc=soc, charging=charging, amps=amps)
    for v in master.vehicleRegistry.vehicles():
        v.claim("TeslaBLE", True, "paired")

    def get_charge_state(vin):
        reads.count += 1
        return {
            "batteryLevel": reads.soc,
            "chargeLimit": limit,
            "chargingState": reads.charging,
            "actualCurrent": reads.amps,
            "availableCurrent": reads.amps,
            "voltage": 240,
            "phases": 1,
        }

    _stub(master, "get_charge_state", get_charge_state)
    return reads


def liveFeed(master, source):
    # A report-on-change feed whose liveness the test turns off (down)
    registry = master.vehicleRegistry
    feed = SimpleNamespace(down=False)
    registry.registerLiveness(source, lambda v: None if feed.down else registry.now())
    return feed


def gen2Slave(master, twcid, state=0x03, amps=0.0):
    # The Gen2 TWC at twcid (made if new), reporting state and amps drawn
    slave = master.newSlave(bytes.fromhex(twcid), 80)
    slave.reportedState, slave.reportedAmpsActual = state, amps
    return slave


def listApi(master, vin, fleet=False, owner=False, signing=None):
    # A real TeslaAPI on the registry's clock with the given token slots,
    # the car listed on it
    clock = master.vehicleRegistry._clock
    api = new_api(master=master)
    api.now, api.wakeDelayMins = clock, 0
    if owner:
        api.storeToken(owner_token())
    if fleet:
        api.storeToken(fleet_token())
    for p in api.providers():
        master.providerOrder.register(p)
        p.clock = clock
    api.addVehicle(
        {"id": 1, "vin": vin, "display_name": "Car", "command_signing": signing}
    )
    return api


def attachFleet(master, vehicle, car):
    assert car.vin == vehicle.vin
    return listApi(master, vehicle.vin, fleet=True)


def attachOwner(master, vehicle, car):
    assert car.vin == vehicle.vin
    return listApi(master, vehicle.vin, owner=True)


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
        module=SimpleNamespace(ping=lambda vin, scan=True: True),
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


def inline(reconciler, kind=InlineQueue):
    # Swaps the reconciler's two queues for InlineQueues (or kind: a plain
    # queue holds every entry until runPending) with the same wiring
    reconciler.ble, reconciler.cloud = (
        kind(
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
