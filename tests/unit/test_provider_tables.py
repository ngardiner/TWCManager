# tests/unit/test_provider_tables.py
import re
from unittest.mock import Mock
import pytest
from TWCManager.Vehicle.Providers import Capability as C
from TWCManager.Vehicle.Providers.Gen2TWCProvider import Gen2TWCProvider
from TWCManager.Vehicle.Providers.Gen3TWCProvider import Gen3TWCProvider
from TWCManager.Vehicle.Providers.HomeAssistantProvider import HomeAssistantProvider
from TWCManager.Vehicle.Providers.Order import ProviderOrder
from TWCManager.Vehicle.Providers.Suppressions import Suppressions
from TWCManager.Vehicle.Providers.TelemetryProvider import TelemetryProvider
from TWCManager.Vehicle.Providers.TeslaAPIProviders import FleetAPIProvider, OwnerAPIProvider
from TWCManager.Vehicle.Providers.TeslaBLEProvider import TeslaBLEProvider
from TWCManager.Vehicle.Reconcile import Outcome as O
from tests.unit.teslatokens import fleet_token, owner_token
from tests.unit.test_carapivehicle_registry import VIN, api_with_registry

class TeslaMateVehicle:                                   # the provider takes its name from the module's class
    def feedLiveAt(self, vehicle=None): return 1.0

def master(settings=None):
    m = Mock(); m.settings = dict(settings or {}); m.config = {}
    m.stats = {"moduleSuccess": {}, "moduleFailures": {}, "moduleDispatch": {}}; return m

def install(kinds, settings):
    # One real provider per kind, all reading one settings dict, and one car
    # every provider can serve
    m = master(settings); api, reg = api_with_registry(); m.vehicleRegistry = reg; o = ProviderOrder(m)
    if "owner" in kinds: api.storeToken(owner_token())
    if "fleet" in kinds: api.storeToken(fleet_token())
    api.master.config = {"config": {}}; api.addVehicle({"id": 1, "vin": VIN, "display_name": "C"}); v = reg.get(VIN)
    for kind in kinds:
        if kind == "gen2":
            slave = Mock(vehicleKey=VIN, minAmps=6, maxOfferAmps=32); slave.minAmpsToOffer.return_value = 6
            gm = Mock(settings=m.settings, desired=None); gm.getSlaveTWCs.return_value = [slave]; o.register(Gen2TWCProvider(gm))
        elif kind == "gen3":
            ctl = Mock(); ctl.devices.return_value = [Mock(vehicleKey=VIN, occupied=True, fuseAmps=48)]; o.register(Gen3TWCProvider(ctl, m))
        elif kind == "ble":
            v.claim("TeslaBLE", True); o.register(TeslaBLEProvider(Mock(), m))
        elif kind == "owner":
            o.register(api.providers()[0])
        elif kind == "fleet":
            o.register(api.providers()[1])
        elif kind == "ha":
            v.attach("HomeAssistant", Mock(has_charge_switch=True, has_charge_current_number=True, has_charge_limit_number=True))
            o.register(HomeAssistantProvider(Mock(enabled=Mock(return_value=True), master=m)))
        elif kind == "telemetry":
            o.register(TelemetryProvider(TeslaMateVehicle()))
    return o, v

def providers_of_every_kind():
    o, v = install(("gen2", "gen3", "ble", "owner", "fleet", "ha", "telemetry"), {}); return o.providers()

SHAPES = {
    "gen2": ("gen2",),
    "gen2+ble": ("gen2", "ble"),
    "gen2+owner+fleet": ("gen2", "owner", "fleet"),
    "gen2+ble+fleet": ("gen2", "ble", "fleet"),
    "gen2+ble+owner+fleet+ha+telemetry": ("gen2", "ble", "owner", "fleet", "ha", "telemetry"),
    "gen3+owner+fleet": ("gen3", "owner", "fleet"),
    "gen3+ble+ha": ("gen3", "ble", "ha"),
}
POLICIES = (None, "prefer_ble", "ble_only", "api_only")

def settingsFor(policy, stopMode):
    s = {"chargeStopMode": str(stopMode)}
    if policy: s["commandPolicy"] = policy
    return s

def legacySuppressed(self, provider, capability):
    # Suppressions.suppressed at ccbe58c, before the tables: the "before" side
    if capability in self.explicit().get(provider.name, frozenset()):
        return True
    settings = getattr(self.master, "settings", None) or {}
    try: localStops = int(settings.get("chargeStopMode", 1)) in (2, 3)
    except (TypeError, ValueError): localStops = False
    if capability == C.STOP_CHARGING and not provider.local and localStops:
        return True
    if capability == C.START_CHARGING and not provider.local and str(settings.get("chargeStopMode")) == "2":
        return True                                                         # Task 12a I2: no cloud start in mode 2, as on main
    policy = self.commandPolicy
    if policy == "ble_only": return not provider.local and capability in C.COMMANDS
    if policy == "api_only": return provider.name == "TeslaBLE"
    return False

def snapshot(o, v):
    names = lambda ps: [p.name for p in ps]
    return {cap: (names(o.ordered(cap)), names(o.candidates(v, cap)), names(o.candidates(v, cap, amps=4)),
                  {p.name: (o.suppressions.suppressed(p, cap), o._ready(p, v, cap), p.available(cap)) for p in o.providers()})
            for cap in sorted(C.ALL)}

@pytest.mark.parametrize("shape", sorted(SHAPES))
@pytest.mark.parametrize("policy", POLICIES)
@pytest.mark.parametrize("stopMode", (1, 2, 3))
def test_order_ready_and_available_match_the_rule_the_tables_replace(shape, policy, stopMode, monkeypatch):
    o, v = install(SHAPES[shape], settingsFor(policy, stopMode)); after = snapshot(o, v)
    monkeypatch.setattr(Suppressions, "suppressed", legacySuppressed); before = snapshot(o, v)
    assert after == before

FULL = SHAPES["gen2+ble+owner+fleet+ha+telemetry"]
EXPECTED = {     # (policy, stopMode) -> candidates at ccbe58c for setAmps below the TWC floor (BLE covers it), stopCharging, wake, readCharge
    (None, 1): (["TeslaBLE"], ["TeslaBLE", "FleetAPI", "HomeAssistant"], ["TeslaBLE", "FleetAPI"], ["TeslaBLE", "OwnerAPI", "HomeAssistant"]),
    ("prefer_ble", 3): (["TeslaBLE"], ["TeslaBLE", "Gen2TWC"], ["TeslaBLE", "FleetAPI"], ["TeslaBLE", "OwnerAPI", "HomeAssistant"]),
    ("ble_only", 1): (["TeslaBLE"], ["TeslaBLE"], ["TeslaBLE"], ["TeslaBLE", "OwnerAPI", "HomeAssistant"]),
    ("api_only", 1): (["FleetAPI", "HomeAssistant"], ["FleetAPI", "HomeAssistant"], ["FleetAPI"], ["OwnerAPI", "HomeAssistant"]),
    ("api_only", 2): (["FleetAPI", "HomeAssistant"], ["Gen2TWC"], ["FleetAPI"], ["OwnerAPI", "HomeAssistant"]),
}

@pytest.mark.parametrize("key", sorted(EXPECTED, key=str))
def test_expected_order_on_a_full_install(key):
    o, v = install(FULL, settingsFor(*key)); names = lambda ps: [p.name for p in ps]
    got = (names(o.candidates(v, C.SET_AMPS, amps=4)), names(o.candidates(v, C.STOP_CHARGING)), names(o.candidates(v, C.WAKE)), names(o.candidates(v, C.READ_CHARGE)))
    assert got == EXPECTED[key]

def test_tables_match_the_declarations_they_replace():
    D = C.Declaration; byName = {p.name: p for p in providers_of_every_kind()}
    for p in byName.values(): assert p.declarations() is type(p).DECLARATIONS
    assert FleetAPIProvider.DECLARATIONS[C.SET_AMPS] == D(C.FLEET_COMMAND)
    assert HomeAssistantProvider.DECLARATIONS[C.STOP_CHARGING] == D(C.HA_BILLED)
    passive = lambda caps, cost: {c: D(cost, passive=True) for c in caps}
    assert byName["Gen2TWC"].declarations() == dict(passive((C.READ_PLUG, C.READ_VIN, C.READ_AMPS), C.LOCAL), **{C.SET_AMPS: D(C.LOCAL), C.START_CHARGING: D(C.LOCAL), C.STOP_CHARGING: D(C.DISRUPTIVE)})
    assert byName["Gen3TWC"].declarations() == dict(passive((C.READ_PLUG, C.READ_AMPS), C.LOCAL), **{c: D(C.LOCAL) for c in (C.SET_AMPS, C.START_CHARGING, C.STOP_CHARGING)})
    assert byName["TeslaBLE"].declarations() == {c: D(C.LOCAL) for c in (C.READ_CHARGE, C.READ_LOCATION, C.READ_PLUG, C.READ_ONLINE, C.SET_CHARGE_LIMIT, C.START_CHARGING, C.STOP_CHARGING, C.SET_AMPS, C.WAKE)}
    wcv = D(C.CLOUD_FREE)
    assert byName["OwnerAPI"].declarations() == dict({c: D(C.CLOUD_FREE) for c in (C.READ_LIST, C.READ_ONLINE, C.READ_CHARGE, C.READ_LOCATION)}, **{c: D(C.OWNER_COMMAND) for c in C.COMMANDS}, **{C.READ_WALL_CONNECTOR_VINS: wcv})
    assert byName["FleetAPI"].declarations() == {C.READ_LIST: D(C.CLOUD_FREE), C.READ_ONLINE: D(C.CLOUD_FREE), C.READ_CHARGE: D(C.FLEET_DATA), C.READ_LOCATION: D(C.FLEET_DATA),
        C.SET_CHARGE_LIMIT: D(C.FLEET_COMMAND), C.START_CHARGING: D(C.FLEET_COMMAND), C.STOP_CHARGING: D(C.FLEET_COMMAND), C.SET_AMPS: D(C.FLEET_COMMAND),
        C.WAKE: D(C.FLEET_WAKE), C.READ_WALL_CONNECTOR_VINS: wcv}
    assert byName["HomeAssistant"].declarations() == {C.READ_CHARGE: D(C.HA_BILLED), C.READ_LOCATION: D(C.HA_BILLED), C.SET_CHARGE_LIMIT: D(C.HA_BILLED), C.SET_AMPS: D(C.HA_BILLED),
        C.START_CHARGING: D(C.HA_BILLED), C.STOP_CHARGING: D(C.HA_BILLED)}
    assert byName["TeslaMateVehicle"].declarations() == passive((C.READ_CHARGE, C.READ_LOCATION, C.READ_PLUG, C.READ_ONLINE), C.CLOUD_FREE)

def test_a_provider_without_a_table_is_suppressed_by_locality():          # pre-flight M5
    m = master({"commandPolicy": "ble_only"}); s = Suppressions(m)
    assert s.suppressed(Mock(local=False, SUPPRESSED=None, name="X"), C.SET_AMPS)
    assert s.suppressed(Mock(spec=["name"], name="X"), C.SET_AMPS)                 # no local and no table at all: cloud, fails closed
    assert not s.suppressed(Mock(local=True, SUPPRESSED=None, name="X"), C.SET_AMPS)
    m.settings.update(commandPolicy="prefer_ble", chargeStopMode="2"); assert s.suppressed(Mock(local=False, name="X"), C.STOP_CHARGING)

def test_suppressions_come_from_the_tables():
    byName = {p.name: p for p in providers_of_every_kind()}; fleet, ble, ha, gen2 = (byName[n] for n in ("FleetAPI", "TeslaBLE", "HomeAssistant", "Gen2TWC"))
    m = master({"commandPolicy": "ble_only"}); s = Suppressions(m)
    assert s.suppressed(fleet, C.SET_AMPS) and not s.suppressed(ble, C.SET_AMPS)
    m.settings.update(commandPolicy="api_only"); assert s.suppressed(ble, C.READ_CHARGE) and not s.suppressed(fleet, C.SET_AMPS)
    m.settings.update(commandPolicy="prefer_ble", chargeStopMode="3"); assert s.suppressed(fleet, C.STOP_CHARGING) and s.suppressed(ha, C.STOP_CHARGING) and not s.suppressed(gen2, C.STOP_CHARGING)
    assert s.conditions() == frozenset({"prefer_ble", "localStops"}) and type(ble).SUPPRESSED == {"api_only": C.ALL}
    m.settings.update(chargeStopMode="x"); assert s.conditions() == frozenset({"prefer_ble"})


# A refused Fleet wake retires wakes only

def calls(requests_mock, part): return [r for r in requests_mock.request_history if part in r.url]

def test_a_403_on_wake_refuses_wakes_only(requests_mock, monkeypatch):     # paid calls counted
    import TWCManager.Vehicle.TeslaAPI as T
    from tests.unit.reconcile_helpers import Clock
    monkeypatch.setattr(T.time, "sleep", Mock())
    clk = Clock(1000.0); api, reg = api_with_registry(); api.addVehicle({"id": 1, "vin": VIN, "display_name": "C"}); api.storeToken(fleet_token())
    api.master.config = {"config": {}}; p = api.providers()[1]; p.clock = clk; api.now = clk; api.wakeDelayMins = 0; car = api.getCarApiVehicles()[0]
    requests_mock.get(re.compile(r".*/api/1/vehicles/%s$" % VIN), json={"response": {"state": "asleep"}})
    requests_mock.post(re.compile(r".*/wake_up$"), status_code=403, json={})
    requests_mock.post(re.compile(r".*/command/.*"), json={"response": {"result": True}})
    assert p.wake(VIN) == O.Outcome(O.UNREACHABLE, "wake refused") and len(calls(requests_mock, "wake_up")) == 1
    free = len(calls(requests_mock, "/vehicles/" + VIN)); clk.t += 4000
    assert p.wake(VIN) == O.Outcome(O.UNREACHABLE, "wake refused") and len(calls(requests_mock, "wake_up")) == 1
    assert len(calls(requests_mock, "/vehicles/" + VIN)) == free + 1                               # the free online check runs first (Task 9 carry b)
    assert O.isNotSent(O.Outcome(O.UNREACHABLE, "wake refused"))
    assert p.setAmps(VIN, 10).kind == O.DONE and api.fleetSlot.usable("command") and p.ready(reg.get(VIN), C.STOP_CHARGING)
    assert api.wakeVehicle(car) is False and len(calls(requests_mock, "wake_up")) == 1           # the legacy wake path too
    requests_mock.get(re.compile(r".*/api/1/vehicles/%s$" % VIN), json={"response": {"state": "online"}}); clk.t += 4000
    assert p.wake(VIN) == O.Outcome(O.SATISFIED, "online") and len(calls(requests_mock, "wake_up")) == 1   # an online car still gets its command
    api.storeToken(fleet_token()); assert api.fleetSlot.usable("wake")                            # a new token lifts it

def test_a_403_on_the_legacy_wake_refuses_wakes_only(requests_mock):        # Task 9 carry (c)
    api, reg = api_with_registry(); api.addVehicle({"id": 1, "vin": VIN, "display_name": "C"}); api.storeToken(fleet_token()); car = api.getCarApiVehicles()[0]
    requests_mock.post(re.compile(r".*/wake_up$"), status_code=403, json={})
    assert api.wakeVehicle(car) is False and len(calls(requests_mock, "wake_up")) == 1
    assert not api.fleetSlot.usable("wake") and api.fleetSlot.usable("command")
    assert api.wakeVehicle(car) is False and len(calls(requests_mock, "wake_up")) == 1                # never sent again to the refused token

@pytest.fixture(autouse=True)
def awake_car(request):
    # The Fleet stop asks the free online check first (Task 12a N2): the car is online
    if "requests_mock" in request.fixturenames:
        request.getfixturevalue("requests_mock").get(re.compile(r".*/api/1/vehicles/5YJ3E1EA1KF000001$"), json={"response": {"state": "online"}})

def test_a_command_403_still_refuses_commands(requests_mock):
    api, reg = api_with_registry(); api.addVehicle({"id": 1, "vin": VIN, "display_name": "C"}); api.storeToken(fleet_token())
    requests_mock.post(re.compile(r".*/command/.*"), status_code=403, json={}); p = api.providers()[1]
    assert p.stopCharging(VIN) == O.Outcome(O.REFUSED, "403") and not api.fleetSlot.usable("command") and api.fleetSlot.usable("read")
