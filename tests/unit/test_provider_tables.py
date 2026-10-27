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
from TWCManager.Vehicle.Providers.TeslaAPIProviders import FleetAPIProvider, OwnerAPIProvider
from TWCManager.Vehicle.Providers.TeslaBLEProvider import TeslaBLEProvider
from TWCManager.Vehicle.Reconcile import Outcome as O
from tests.unit.teslatokens import fleet_token, owner_token
from tests.unit.test_teslaapi_registry import VIN, api_with_registry

def master(settings=None):
    m = Mock(); m.settings = dict(settings or {}); m.config = {}
    return m

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
    return o, v

def providers_of_every_kind():
    o, v = install(("gen2", "gen3", "ble", "owner", "fleet", "ha"), {}); return o.providers()

SHAPES = {
    "gen2": ("gen2",),
    "gen2+ble": ("gen2", "ble"),
    "gen2+owner+fleet": ("gen2", "owner", "fleet"),
    "gen2+ble+fleet": ("gen2", "ble", "fleet"),
    "gen2+ble+owner+fleet+ha": ("gen2", "ble", "owner", "fleet", "ha"),
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

FULL = SHAPES["gen2+ble+owner+fleet+ha"]
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
    byName = {p.name: p for p in providers_of_every_kind()}
    for p in byName.values(): assert p.declarations() is type(p).DECLARATIONS
    assert FleetAPIProvider.DECLARATIONS[C.SET_AMPS] == C.FLEET_COMMAND
    assert HomeAssistantProvider.DECLARATIONS[C.STOP_CHARGING] == C.HA_BILLED
    assert byName["Gen2TWC"].declarations() == {C.SET_AMPS: C.LOCAL, C.START_CHARGING: C.LOCAL, C.STOP_CHARGING: C.DISRUPTIVE}
    assert byName["Gen3TWC"].declarations() == {c: C.LOCAL for c in (C.SET_AMPS, C.START_CHARGING, C.STOP_CHARGING)}
    assert byName["TeslaBLE"].declarations() == {c: C.LOCAL for c in (C.READ_CHARGE, C.SET_CHARGE_LIMIT, C.START_CHARGING, C.STOP_CHARGING, C.SET_AMPS, C.WAKE)}
    wcv = C.CLOUD_FREE
    assert byName["OwnerAPI"].declarations() == {C.READ_CHARGE: C.CLOUD_FREE,
        C.SET_CHARGE_LIMIT: C.OWNER_COMMAND, C.START_CHARGING: C.OWNER_COMMAND, C.STOP_CHARGING: C.OWNER_COMMAND, C.SET_AMPS: C.OWNER_COMMAND,
        C.WAKE: C.OWNER_COMMAND, C.READ_WALL_CONNECTOR_VINS: wcv}   # #659: unsigned cars only
    assert byName["FleetAPI"].declarations() == {C.READ_CHARGE: C.FLEET_DATA,
        C.SET_CHARGE_LIMIT: C.FLEET_COMMAND, C.START_CHARGING: C.FLEET_COMMAND, C.STOP_CHARGING: C.FLEET_COMMAND, C.SET_AMPS: C.FLEET_COMMAND,
        C.WAKE: C.FLEET_WAKE, C.READ_WALL_CONNECTOR_VINS: wcv}
    assert byName["HomeAssistant"].declarations() == {C.READ_CHARGE: C.HA_BILLED, C.SET_CHARGE_LIMIT: C.HA_BILLED, C.SET_AMPS: C.HA_BILLED,
        C.START_CHARGING: C.HA_BILLED, C.STOP_CHARGING: C.HA_BILLED}

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
    api.master.config = {"config": {}}; p = api.providers()[1]; p.clock = clk; api.now = clk; api.wakeDelayMins = 0
    requests_mock.get(re.compile(r".*/api/1/vehicles/%s$" % VIN), json={"response": {"state": "asleep"}})
    requests_mock.post(re.compile(r".*/wake_up$"), status_code=403, json={})
    requests_mock.post(re.compile(r".*/command/.*"), json={"response": {"result": True}})
    assert p.wake(VIN) == O.Outcome(O.UNREACHABLE, "wake refused") and len(calls(requests_mock, "wake_up")) == 1
    free = len(calls(requests_mock, "/vehicles/" + VIN)); clk.t += 4000
    assert p.wake(VIN) == O.Outcome(O.UNREACHABLE, "wake refused") and len(calls(requests_mock, "wake_up")) == 1
    assert len(calls(requests_mock, "/vehicles/" + VIN)) == free + 1                               # the free online check runs first (Task 9 carry b)
    assert O.isNotSent(O.Outcome(O.UNREACHABLE, "wake refused"))
    assert p.setAmps(VIN, 10).kind == O.DONE and api.fleetSlot.usable("command") and p.ready(reg.get(VIN), C.STOP_CHARGING)
    requests_mock.get(re.compile(r".*/api/1/vehicles/%s$" % VIN), json={"response": {"state": "online"}}); clk.t += 4000
    assert p.wake(VIN) == O.Outcome(O.SATISFIED, "online") and len(calls(requests_mock, "wake_up")) == 1   # an online car still gets its command
    api.storeToken(fleet_token()); assert api.fleetSlot.usable("wake")                            # a new token lifts it

@pytest.fixture(autouse=True)
def awake_car(request):
    # The Fleet stop asks the free online check first (Task 12a N2): the car is online
    if "requests_mock" in request.fixturenames:
        request.getfixturevalue("requests_mock").get(re.compile(r".*/api/1/vehicles/5YJ3E1EA1KF000001$"), json={"response": {"state": "online"}})

def test_a_command_403_still_refuses_commands(requests_mock):
    api, reg = api_with_registry(); api.addVehicle({"id": 1, "vin": VIN, "display_name": "C"}); api.storeToken(fleet_token())
    requests_mock.post(re.compile(r".*/command/.*"), status_code=403, json={}); p = api.providers()[1]
    assert p.stopCharging(VIN) == O.Outcome(O.REFUSED, "403") and not api.fleetSlot.usable("command") and api.fleetSlot.usable("read")
