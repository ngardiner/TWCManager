# tests/unit/test_vehicle_endpoint.py
from types import SimpleNamespace
from unittest.mock import Mock
from TWCManager.EVSEController.VehicleEndpoints import VehicleEndpoints
from TWCManager.EVSEInstance.VehicleEndpoint import VehicleEndpoint
from TWCManager.Vehicle.Providers import Capability as C
from TWCManager.Vehicle.Providers.Order import ProviderOrder
from TWCManager.Vehicle.Registry.VehicleRegistry import VehicleRegistry
from tests.unit.test_provider_order import ble, fleet, twc
from tests.unit.test_carapivehicle_registry import api_with_registry
from tests.unit.teslatokens import fleet_token, owner_token
VIN1 = "5YJ3E1EA1KF000001"; VIN2 = "5YJ3E1EA1KF000002"

class Clock:
    def __init__(self, t=1000.0): self.t = t
    def __call__(self): return self.t

def master(settings=None, *providers, slaves=()):
    m = Mock(); m.settings = settings or {}; m.config = {"config": {"wiringMaxAmpsPerTWC": 32, "defaultVoltage": 240}}
    m.stats = {"moduleSuccess": {}, "moduleFailures": {}}; m.vehicleRegistry = VehicleRegistry({}); m.providerOrder = ProviderOrder(m)
    for p in providers: m.providerOrder.register(p)
    carapi = Mock(); carapi.enabled.return_value = True
    m.getModuleByName = lambda n: carapi if n == "TeslaAPI" else None; m.getSlaveTWCs = lambda: list(slaves); return m

def car(m, vin, atHome=True, syncSource="TeslaAPI", pluggedIn=None):
    v = m.vehicleRegistry.getOrCreate(vin, "TeslaAPI"); v.attach("TeslaAPI", SimpleNamespace(VIN=vin, atHome=atHome, syncSource=syncSource, pluggedIn=pluggedIn)); return v

def slave(key=None, amps=0.0, state=0x00): return SimpleNamespace(vehicleKey=key, reportedAmpsActual=amps, reportedState=state)

def test_read_only_when_no_amps_candidate_and_ble_only_excludes_fleet():   # A17
    m = master({"commandPolicy": "ble_only"}, fleet()); e = VehicleEndpoint(car(m, VIN1), m.providerOrder, m)
    assert e.isReadOnly and e.provider == ""
    m2 = master({}, fleet()); e2 = VehicleEndpoint(car(m2, VIN1), m2.providerOrder, m2)
    assert not e2.isReadOnly and e2.provider == "FleetAPI" and e2.ID == "Vehicle-" + VIN1 and e2.vehicleKey == VIN1

def test_read_only_follows_fleet_slot():
    # Moved from test_teslaapi_readonly_gate's old EVSE case: an Owner-only
    # TeslaAPI leaves FleetAPIProvider not ready, so the endpoint is read-only
    api, reg = api_with_registry()
    api.addVehicle({"id": 1, "vin": VIN1, "display_name": "Callandor"})
    order = ProviderOrder(api.master)
    for p in api.providers(): order.register(p)
    api.fleetSlot.clear(); api.storeToken(owner_token())
    e = VehicleEndpoint(reg.get(VIN1), order, api.master)
    assert e.isReadOnly is True and e.provider == ""
    api.storeToken(fleet_token())
    assert e.isReadOnly is False and e.provider == "FleetAPI"

def test_state_from_registry_values_without_reads():
    m = master({}, fleet()); v = car(m, VIN1); e = VehicleEndpoint(v, m.providerOrder, m); v.read = Mock(side_effect=AssertionError("no reads"))
    assert not e.wantsToCharge and e.occupied is None and e.maxPower == 32 * 240 and e.minPower == 5 * 240
    v.observe("chargingState", "Stopped", "TeslaAPI"); v.observe("availableCurrent", 16, "TeslaAPI"); v.observe("actualCurrent", 0, "TeslaAPI")
    assert e.wantsToCharge and not e.isCharging and e.occupied is True and e.maxPower == 16 * 240

def test_set_target_power_queues_on_change_or_after_refresh():
    c = Clock(); m = master({}, fleet()); e = VehicleEndpoint(car(m, VIN1), m.providerOrder, m, clock=c)
    e.setTargetPower(0); m.queue_vehicle_command.assert_not_called()
    e.setTargetPower(2400); e.setTargetPower(2400)
    m.queue_vehicle_command.assert_called_once_with({"cmd": "setAmps", "vin": VIN1, "amps": 10})
    c.t += 600; e.setTargetPower(2400); assert m.queue_vehicle_command.call_count == 2
    e.setTargetPower(100); assert m.queue_vehicle_command.call_args.args[0]["amps"] == 1

def test_controller_lists_at_home_api_cars_and_keeps_blind_rules():
    m = master({}, fleet()); car(m, VIN1); car(m, VIN2, atHome=False); m.vehicleRegistry.beginPlugSession("8244")
    ctl = VehicleEndpoints(m)
    assert [e.currentVIN for e in ctl.allEVSEs] == [VIN1] and ctl.allEVSEs[0] is ctl.allEVSEs[0]
    m.getSlaveTWCs = lambda: [slave(amps=5.0)]; assert ctl.allEVSEs == []                                 # blind VIN-less TWC
    m.getSlaveTWCs = lambda: [slave()]; car(m, VIN2, pluggedIn=True, syncSource="TeslaMateVehicle"); m.vehicleRegistry.get(VIN2).attachment("TeslaAPI").atHome = True
    assert [e.currentVIN for e in ctl.allEVSEs] == [VIN1]                                                  # unknown TWC + telemetry plug
    m.getSlaveTWCs = lambda: [slave(key=VIN1)]
    assert sorted(e.currentVIN for e in ctl.allEVSEs) == [VIN1, VIN2]                                      # named TWC: grouping merges, not exclusion

def test_last_vin_plugged_twc_keeps_other_cars_listed():                           # Review Focus 5 / I5
    m = master({}, fleet()); car(m, VIN1); car(m, VIN2)
    m.getSlaveTWCs = lambda: [slave(key=VIN1, state=0x03)]                      # stopped car: currentVIN cleared, lastVIN attributed
    assert sorted(e.currentVIN for e in VehicleEndpoints(m).allEVSEs) == [VIN1, VIN2]
    m.getSlaveTWCs = lambda: [slave(key="anon:8244", state=0x03)]
    assert VehicleEndpoints(m).allEVSEs == []

def test_controller_empty_without_tesla_api():
    m = master({}, ble()); m.getModuleByName = lambda n: None; car(m, VIN1)
    assert VehicleEndpoints(m).allEVSEs == []

def test_anonymous_car_on_a_gen3_is_blind_but_a_correlated_one_is_not():           # G3
    m = master({}, fleet()); car(m, VIN1); carapi = m.getModuleByName("TeslaAPI")
    gen3 = Mock(); gen3.devices = lambda: [SimpleNamespace(occupied=True, vehicleKey="anon:Gen3TWC-10.0.0.1")]
    m.getModuleByName = lambda n: {"TeslaAPI": carapi, "Gen3TWCs": gen3}.get(n)
    assert VehicleEndpoints(m).allEVSEs == []
    gen3.devices = lambda: [SimpleNamespace(occupied=True, vehicleKey=VIN1)]
    assert [e.currentVIN for e in VehicleEndpoints(m).allEVSEs] == [VIN1]

def test_occupied_gen3_without_a_vehicle_key_is_blind():                           # Task 10 review
    m = master({}, fleet()); car(m, VIN1); carapi = m.getModuleByName("TeslaAPI")
    gen3 = Mock(); gen3.devices = lambda: [SimpleNamespace(occupied=True, vehicleKey=None)]
    m.getModuleByName = lambda n: {"TeslaAPI": carapi, "Gen3TWCs": gen3}.get(n)
    assert VehicleEndpoints(m).allEVSEs == []
    gen3.devices = lambda: [SimpleNamespace(occupied=None, vehicleKey=None), SimpleNamespace(occupied=False, vehicleKey=None)]
    assert [e.currentVIN for e in VehicleEndpoints(m).allEVSEs] == [VIN1]

def test_a_car_only_ble_can_serve_is_writable_over_ble():                          # Task 10 review
    for m in (master({}, ble()), master({"commandPolicy": "ble_only"}, ble(), fleet())):
        e = VehicleEndpoint(car(m, VIN1), m.providerOrder, m)
        assert not e.isReadOnly and e.provider == "TeslaBLE"
