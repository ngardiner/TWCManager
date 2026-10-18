# tests/unit/test_vehicle_endpoint.py
from types import SimpleNamespace
from unittest.mock import Mock
from TWCManager.EVSEInstance.VehicleEndpoint import VehicleEndpoint, _blindTWCs, vehicleEndpoints
from TWCManager.Vehicle.Providers import Capability as C
from TWCManager.Vehicle.Providers.Order import ProviderOrder
from TWCManager.Vehicle.Registry.VehicleRegistry import VehicleRegistry
from tests.unit.test_provider_order import ble, fleet, twc
from tests.unit.test_teslaapi_registry import api_with_registry
from tests.unit.teslatokens import fleet_token, owner_token
VIN1 = "5YJ3E1EA1KF000001"; VIN2 = "5YJ3E1EA1KF000002"

def master(settings=None, *providers, slaves=()):
    m = Mock(); m.settings = settings or {}; m.config = {"config": {"wiringMaxAmpsPerTWC": 32, "defaultVoltage": 240}}
    m.vehicleRegistry = VehicleRegistry({}); m.providerOrder = ProviderOrder(m)
    for p in providers: m.providerOrder.register(p)
    carapi = Mock(); carapi.enabled.return_value = True; carapi.locate.return_value = None
    m.getModuleByName = lambda n: carapi if n == "TeslaAPI" else None; m.getSlaveTWCs = lambda: list(slaves)
    m.getModulesByType = lambda t: []                                            # no RS-485 interface loaded
    return m

def car(m, vin, atHome=True):
    v = m.vehicleRegistry.getOrCreate(vin, "OwnerAPI"); v.attach("TeslaAPI", SimpleNamespace(VIN=vin, atHome=atHome)); return v

def provider(e):
    # The provider that would set this endpoint's rate ("" for none)
    candidates = e._candidates(); return candidates[0].name if candidates else ""

def slave(key=None, amps=0.0, state=0x00): return SimpleNamespace(vehicleKey=key, reportedAmpsActual=amps, reportedState=state)

def test_read_only_when_no_amps_candidate_and_ble_only_excludes_fleet():
    m = master({"commandPolicy": "ble_only"}, fleet()); e = VehicleEndpoint(car(m, VIN1), m.providerOrder, m)
    assert e.isReadOnly and provider(e) == ""
    m2 = master({}, fleet()); e2 = VehicleEndpoint(car(m2, VIN1), m2.providerOrder, m2)
    assert not e2.isReadOnly and provider(e2) == "FleetAPI" and e2.ID == "Vehicle-" + VIN1 and e2.vehicleKey == VIN1

def test_read_only_follows_fleet_slot():
    # Moved from test_teslaapi_readonly_gate's old EVSE case: an Owner-only
    # TeslaAPI leaves FleetAPIProvider not ready, so the endpoint is read-only
    api, reg = api_with_registry()
    api.addVehicle({"id": 1, "vin": VIN1, "display_name": "TestCar"})
    order = ProviderOrder(api.master)
    for p in api.providers(): order.register(p)
    api.fleetSlot.clear(); api.storeToken(owner_token())
    e = VehicleEndpoint(reg.get(VIN1), order, api.master)
    assert e.isReadOnly is True and provider(e) == ""
    api.storeToken(fleet_token())
    assert e.isReadOnly is False and provider(e) == "FleetAPI"

def test_state_from_registry_values_without_reads():
    m = master({}, fleet()); v = car(m, VIN1); e = VehicleEndpoint(v, m.providerOrder, m); v.read = Mock(side_effect=AssertionError("no reads"))
    assert not e.wantsToCharge and e.occupied is None and e.maxPower == 32 * 240 and e.minPower == 5 * 240
    v.observe("chargingState", "Stopped", "OwnerAPI"); v.observe("availableCurrent", 16, "OwnerAPI"); v.observe("actualCurrent", 0, "OwnerAPI")
    assert e.wantsToCharge and e.occupied is True and e.maxPower == 32 * 240   # the pilot is not the car's limit
    v.observe("chargeCurrentRequestMax", 16, "OwnerAPI"); assert e.maxPower == 16 * 240

def test_set_target_power_sets_desired_amps():
    from TWCManager.Vehicle.Reconcile.Desired import AMPS, DesiredStore
    m = master({}, fleet()); m.desired = DesiredStore(); e = VehicleEndpoint(car(m, VIN1), m.providerOrder, m)
    e.setTargetPower(0); assert m.desired.get(VIN1, AMPS) is None
    e.setTargetPower(2400); d = m.desired.get(VIN1, AMPS); assert (d.value, d.by) == (10, "distributor")
    e.setTargetPower(100); assert m.desired.get(VIN1, AMPS).value == 1                  # vehicle amps are never 0
    e.setTargetPower(0); assert m.desired.get(VIN1, AMPS) is None                        # a zero offer withdraws the rate

def test_lists_at_home_api_cars_and_keeps_blind_rules():
    m = master({}, fleet()); car(m, VIN1); car(m, VIN2, atHome=False); m.vehicleRegistry.beginPlugSession("8244")
    assert [e.currentVIN for e in vehicleEndpoints(m)] == [VIN1]
    m.getSlaveTWCs = lambda: [slave(amps=5.0)]; assert vehicleEndpoints(m) == []                                 # blind VIN-less TWC
    m.getSlaveTWCs = lambda: [slave()]; v2 = car(m, VIN2); v2.observe("state", "online", "TeslaMateVehicle"); v2.observe("pluggedIn", True, "TeslaMateVehicle"); m.vehicleRegistry.get(VIN2).attachment("TeslaAPI").atHome = True
    assert [e.currentVIN for e in vehicleEndpoints(m)] == [VIN1]                                                  # unknown TWC + telemetry plug
    m.getSlaveTWCs = lambda: [slave(key=VIN1)]
    assert sorted(e.currentVIN for e in vehicleEndpoints(m)) == [VIN1, VIN2]                                      # named TWC: grouping merges, not exclusion

def test_last_vin_plugged_twc_keeps_other_cars_listed():
    m = master({}, fleet()); car(m, VIN1); car(m, VIN2)
    m.getSlaveTWCs = lambda: [slave(key=VIN1, state=0x03)]                      # stopped car: currentVIN cleared, lastVIN attributed
    assert sorted(e.currentVIN for e in vehicleEndpoints(m)) == [VIN1, VIN2]
    m.getSlaveTWCs = lambda: [slave(key="anon:8244", state=0x03)]
    assert vehicleEndpoints(m) == []

def test_empty_without_tesla_api():
    m = master({}, ble()); m.getModuleByName = lambda n: None; car(m, VIN1)
    assert vehicleEndpoints(m) == []

def test_anonymous_car_on_a_gen3_is_blind_but_a_correlated_one_is_not():
    m = master({}, fleet()); car(m, VIN1); carapi = m.getModuleByName("TeslaAPI")
    gen3 = Mock(); gen3.devices = lambda: [SimpleNamespace(occupied=True, vehicleKey="anon:Gen3TWC-10.0.0.1")]
    m.getModuleByName = lambda n: {"TeslaAPI": carapi, "Gen3TWCs": gen3}.get(n)
    assert vehicleEndpoints(m) == []
    gen3.devices = lambda: [SimpleNamespace(occupied=True, vehicleKey=VIN1)]
    assert [e.currentVIN for e in vehicleEndpoints(m)] == [VIN1]

def test_occupied_gen3_without_a_vehicle_key_is_blind():
    m = master({}, fleet()); car(m, VIN1); carapi = m.getModuleByName("TeslaAPI")
    gen3 = Mock(); gen3.devices = lambda: [SimpleNamespace(occupied=True, vehicleKey=None)]
    m.getModuleByName = lambda n: {"TeslaAPI": carapi, "Gen3TWCs": gen3}.get(n)
    assert vehicleEndpoints(m) == []
    gen3.devices = lambda: [SimpleNamespace(occupied=None, vehicleKey=None), SimpleNamespace(occupied=False, vehicleKey=None)]
    assert [e.currentVIN for e in vehicleEndpoints(m)] == [VIN1]

def test_a_car_only_ble_can_serve_is_writable_over_ble():
    for m in (master({}, ble()), master({"commandPolicy": "ble_only"}, ble(), fleet())):
        e = VehicleEndpoint(car(m, VIN1), m.providerOrder, m)
        assert not e.isReadOnly and provider(e) == "TeslaBLE"

# Coverage carried over from the old TeslaAPI controller tests

def test_a_failed_scan_fails_closed():
    m = master({}, fleet()); car(m, VIN1)
    def broken(): raise RuntimeError("scan")
    m.getSlaveTWCs = broken
    assert _blindTWCs(m) == (True, True) and vehicleEndpoints(m) == []
    m.getSlaveTWCs = lambda: []; carapi = m.getModuleByName("TeslaAPI")
    gen3 = Mock(); gen3.devices = broken                                          # the Gen3 walk is inside the same guard
    m.getModuleByName = lambda n: {"TeslaAPI": carapi, "Gen3TWCs": gen3}.get(n)
    assert vehicleEndpoints(m) == []

def test_just_below_power_flowing_is_not_blind():
    m = master({}, fleet()); car(m, VIN1)
    m.getSlaveTWCs = lambda: [slave(amps=0.99)]
    assert [e.currentVIN for e in vehicleEndpoints(m)] == [VIN1]
    m.getSlaveTWCs = lambda: [slave(amps=1.0)]
    assert vehicleEndpoints(m) == []

def test_disabled_tesla_api_lists_nothing():
    m = master({}, fleet()); car(m, VIN1); m.getModuleByName("TeslaAPI").enabled.return_value = False
    assert vehicleEndpoints(m) == []

def test_a_hidden_car_is_listed_again_once_visible():
    m = master({}, fleet()); car(m, VIN1)
    (first,) = vehicleEndpoints(m)
    m.getSlaveTWCs = lambda: [slave(amps=5.0)]; assert vehicleEndpoints(m) == []
    m.getSlaveTWCs = lambda: []; (again,) = vehicleEndpoints(m)
    assert again.currentVIN == first.currentVIN == VIN1
