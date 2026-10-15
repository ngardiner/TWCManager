import logging
from unittest.mock import Mock
import pytest
from TWCManager.Vehicle.Providers import Capability as C
from TWCManager.Vehicle.Providers.Order import ProviderOrder
from TWCManager.Vehicle.Providers.Provider import Provider
from TWCManager.Vehicle.Providers.Suppressions import Suppressions
from TWCManager.Vehicle.Reconcile import Outcome as O
from TWCManager.Vehicle.Registry.ReadSource import ReadSource
from TWCManager.Vehicle.Registry.VehicleRegistry import VehicleRegistry
VIN = "5YJ3E1EA1KF000001"

class P(Provider):
    def __init__(self, name, rank, local, caps, ready=True, amps=None, readTier=None):
        self.name, self.rank, self.local, self._caps, self._ready, self._amps = name, rank, local, caps, ready, amps
        self.calls = []; self.module = Mock()          # dispatch skips module-less (TWC) providers
        if readTier is not None:
            self.readSource = ReadSource(); self.readSource.name = name; self.readSource.provider = name
            self._caps = dict(self._caps, **{C.READ_CHARGE: C.Declaration(C.Cost(readTier))})
    def declarations(self): return self._caps
    def ready(self, vehicle, cap): return self._ready
    def ampsRange(self, vehicle): return self._amps
    def setAmps(self, vin, amps): self.calls.append(("setAmps", vin, amps)); return self._result
    def wake(self, vin): self.calls.append(("wake", vin)); return self._result
    _result = True

def twc(ready=True, amps=(6, 32)):  return P("Gen2TWC", 0, True, {C.SET_AMPS: C.Declaration(C.LOCAL)}, ready, amps)
def ble(ready=True):
    b = P("TeslaBLE", 2, True, {C.SET_AMPS: C.Declaration(C.LOCAL), C.WAKE: C.Declaration(C.LOCAL)}, ready, (1, 32), readTier=0)
    b.SUPPRESSED = {"api_only": C.ALL}; return b                  # the real provider's table; the others keep the locality rule
def owner():          return P("OwnerAPI", 4, False, {}, readTier=1)
def fleet(ready=True): return P("FleetAPI", 5, False, {C.SET_AMPS: C.Declaration(C.FLEET_COMMAND), C.WAKE: C.Declaration(C.FLEET_WAKE)}, ready, (1, 32), readTier=2)
def ha(ready=True):   return P("HomeAssistant", 6, False, {C.SET_AMPS: C.Declaration(C.HA_BILLED)}, ready, (1, 32), readTier=2)

def master(settings=None, config=None):
    m = Mock(); m.settings = settings or {}; m.config = config or {}
    m.stats = {"moduleSuccess": {}, "moduleFailures": {}, "moduleDispatch": {}}
    m.vehicleRegistry = VehicleRegistry({}); return m

def order(m, *providers):
    o = ProviderOrder(m)
    for p in providers: o.register(p)
    return o

def test_fixed_order_is_tier_then_rank_regardless_of_registration():
    o = order(master(), ha(), fleet(), ble(), twc())
    assert [p.name for p in o.ordered(C.SET_AMPS)] == ["Gen2TWC", "TeslaBLE", "FleetAPI", "HomeAssistant"]
    assert [p.name for p in o.ordered(C.WAKE)] == ["TeslaBLE", "FleetAPI"]

def test_candidates_require_readiness_and_ordered_requires_availability():
    o = order(master(), twc(ready=False), ble(ready=False), fleet())
    assert [p.name for p in o.candidates(None, C.SET_AMPS)] == ["FleetAPI"]
    f = fleet(); f.available = lambda cap: cap != C.SET_AMPS
    assert order(master(), f).ordered(C.SET_AMPS) == [] and [p.name for p in order(master(), f).ordered(C.WAKE)] == ["FleetAPI"]

def test_dispatch_skips_providers_without_a_module():
    t = twc(); t.module = None; o = order(master(), t, fleet())
    assert o.dispatch(None, C.SET_AMPS, lambda p: p.setAmps(VIN, 10), amps=10) is False and t.calls == []   # TWC path exists, so Fleet is dropped; TWC is never written

def test_default_fleet_amps_rule_uses_target_and_range():      # Review Focus 3
    o = order(master(), twc(), fleet())
    assert [p.name for p in o.candidates(None, C.SET_AMPS)] == ["Gen2TWC"]           # unknown target: local path exists
    assert [p.name for p in o.candidates(None, C.SET_AMPS, amps=10)] == ["Gen2TWC"]  # covered: Fleet dropped
    assert [p.name for p in o.candidates(None, C.SET_AMPS, amps=4)] == ["FleetAPI"]  # below the TWC floor: Fleet allowed
    o = order(master(), fleet())
    assert [p.name for p in o.candidates(None, C.SET_AMPS, amps=10)] == ["FleetAPI"] # UMC car: Fleet is the only path

def test_ble_only_suppresses_remote_commands_not_reads():
    o = order(master({"commandPolicy": "ble_only"}), ble(), fleet(), ha())
    assert [p.name for p in o.ordered(C.SET_AMPS)] == ["TeslaBLE"]
    assert [s.name for s in o.orderReadSources([p.readSource for p in (ha(), fleet(), ble())])] == ["TeslaBLE", "FleetAPI", "HomeAssistant"]

def test_api_only_suppresses_ble_for_everything(caplog):        # Review Focus 1
    m = master({"commandPolicy": "api_only"}); o = order(m, ble(), fleet())
    assert [p.name for p in o.ordered(C.SET_AMPS)] == ["FleetAPI"]
    assert [s.name for s in o.orderReadSources([ble().readSource, fleet().readSource])] == ["FleetAPI"]
    with caplog.at_level(logging.INFO):
        o.suppressions.noticeOnce(); o.suppressions.noticeOnce()
    assert sum("api_only" in r.message for r in caplog.records) == 1

def test_config_policy_overrides_settings():
    s = Suppressions(master({"commandPolicy": "ble_only"}, {"vehicle": {"commandPolicy": "api_only"}}))
    assert s.commandPolicy == "api_only" and s.commandPolicyOverridden
    assert Suppressions(master({"commandPolicy": "banana"})).commandPolicy == "prefer_ble"

def test_explicit_suppressions_per_provider_and_capability():
    o = order(master({"providerSuppressions": {"FleetAPI": ["setAmps"], "HomeAssistant": ["*"]}}), fleet(), ha())
    assert o.ordered(C.SET_AMPS) == [] and [p.name for p in o.ordered(C.WAKE)] == ["FleetAPI"]

@pytest.mark.parametrize("bad", ["x", ["FleetAPI"], {"FleetAPI": "setAmps"}, {"Nope": ["setAmps"]}, {"FleetAPI": ["fly"]}, {"FleetAPI": [3]}])
def test_malformed_suppressions_are_ignored(bad):                 # Review Focus 4
    o = order(master({"providerSuppressions": bad}), fleet())
    assert [p.name for p in o.ordered(C.SET_AMPS)] == ["FleetAPI"]

def test_read_sources_ordered_by_tier_then_rank_and_unknown_last():
    o = order(master(), fleet(), owner(), ble())
    other = ReadSource(); other.name = "X"; other.order = 0
    got = o.orderReadSources([other, fleet().readSource, owner().readSource, ble().readSource])
    assert [s.name for s in got] == ["TeslaBLE", "OwnerAPI", "FleetAPI", "X"]

def test_registry_uses_installed_read_order():
    r = VehicleRegistry({}); a = ReadSource(); a.name = "a"; a.order = 1; b = ReadSource(); b.name = "b"; b.order = 0
    r.registerReadSource(a); r.registerReadSource(b)
    assert [s.name for s in r.readSources()] == ["b", "a"]
    r.useReadOrder(lambda sources: list(sources))                 # registration order, no sort
    assert [s.name for s in r.readSources()] == ["a", "b"] and r.useReadOrder(None) is None
    assert [s.name for s in r.readSources()] == ["b", "a"]

def test_dispatch_walks_candidates_with_attempts_and_stats():
    m = master(); b = ble(); b._result = False; f = fleet()
    o = order(m, b, f)
    assert o.dispatch(None, C.WAKE, lambda p: p.wake(VIN)) is True
    assert b.calls == [("wake", VIN)] * 3 and f.calls == [("wake", VIN)]
    assert m.stats["moduleFailures"] == {"TeslaBLE": 1} and m.stats["moduleSuccess"] == {"FleetAPI": 1}

def test_dispatch_treats_error_and_exceptions_as_failures():
    b = ble(); b._result = "error"; f = fleet(); f.wake = Mock(side_effect=RuntimeError("secret-token"))
    o = order(master(), b, f)
    assert o.dispatch(None, C.WAKE, lambda p: p.wake(VIN)) is False and f.wake.call_count == 2

def workers_master():
    from TWCManager.Vehicle.Reconcile.Run import Workers
    m = master(); m.vehicleRegistry.getOrCreate(VIN, "t"); del m.reconciler; m.workers = Workers(m, inline=True); return m

def test_debug_command_goes_through_the_order():
    m = workers_master(); f = fleet(); f._result = O.Outcome(O.DONE); o = order(m, f); m.providerOrder = o
    assert o.debugCommand("setChargeRate", VIN, '{"charge_rate": "12"}') is True and f.calls == [("setAmps", VIN, 12)]
    assert o.debugCommand("setChargeRate", VIN, '{"charge_rate": 0}') is True and f.calls[-1] == ("setAmps", VIN, 1)
    assert o.debugCommand("wakeVehicle", VIN, "not json") is True and f.calls[-1] == ("wake", VIN)
    assert o.debugCommand("wakeVehicle", "5YJ3E1EA1KF000009", "{}") is False and o.debugCommand("honk", VIN, "{}") is False
    assert len(f.calls) == 3

def test_debug_command_without_workers_submits_nothing():
    m = master(); m.vehicleRegistry.getOrCreate(VIN, "t"); m.workers = None; f = fleet(); o = order(m, f)
    assert o.debugCommand("wakeVehicle", VIN, "{}") is False and f.calls == []

@pytest.mark.parametrize("params", ['{"charge_rate": "inf"}', '{"charge_rate": 1e999}', '{"charge_rate": "-inf"}'])
def test_debug_command_refuses_an_infinite_rate(params):
    m = master(); m.vehicleRegistry.getOrCreate(VIN, "t"); f = fleet(); o = order(m, f)
    assert o.debugCommand("setChargeRate", VIN, params) is False and f.calls == []

def test_debug_command_tries_each_provider_once():
    # User-initiated: one provider per command, never sent twice, no fall-through
    m = workers_master(); b = ble(); b._result = O.Outcome(O.UNREACHABLE, "timeout"); f = fleet(); f._result = O.Outcome(O.DONE)
    o = order(m, b, f); m.providerOrder = o
    assert o.debugCommand("wakeVehicle", VIN, "{}") is True and b.calls == [("wake", VIN)] and f.calls == []
    assert o.debugCommand("setChargeRate", VIN, '{"charge_rate": 3}') is True and b.calls[1:] == [("setAmps", VIN, 3)] and f.calls == []

def test_debug_set_charge_rate_reaches_fleet_past_a_covering_twc():         # M1
    # As the facade's setChargeRate: the TWC cannot lift the car's own limit
    m = workers_master(); f = fleet(); f._result = O.Outcome(O.DONE)
    t = twc(); t.module = None                      # as Gen2TWCProvider: never written from the HTTP thread
    o = order(m, t, f); m.providerOrder = o
    assert o.debugCommand("setChargeRate", VIN, '{"charge_rate": 16}') is True and f.calls == [("setAmps", VIN, 16)] and t.calls == []
    f._result = True
    assert o.dispatch(m.vehicleRegistry.get(VIN), C.SET_AMPS, lambda p: p.setAmps(VIN, 16), amps=16) is False   # setAmps keeps the rule
