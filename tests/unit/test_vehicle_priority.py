# tests/unit/test_vehicle_priority.py
from unittest.mock import Mock
from TWCManager.Vehicle.Providers import Capability as C
from TWCManager.Vehicle.Providers.Order import ProviderOrder
from TWCManager.Vehicle.Providers.Provider import Provider
from TWCManager.Vehicle.Registry.VehicleRegistry import VehicleRegistry
from TWCManager.Vehicle.VehiclePriority import VehiclePriority
VIN = "5YJ3E1EA1KF000001"
CMDS = {c: C.Declaration(C.LOCAL) for c in (C.SET_AMPS, C.START_CHARGING, C.STOP_CHARGING, C.SET_CHARGE_LIMIT, C.WAKE)}

class P(Provider):
    def __init__(self, name, rank, local, module, ready=True, caps=CMDS, amps=(1, 32)):
        self.name, self.rank, self.local, self.module, self._ready, self._caps, self._amps = name, rank, local, module, ready, caps, amps
    def declarations(self): return self._caps
    def ready(self, vehicle, cap): return self._ready
    def ampsRange(self, vehicle): return self._amps
    def setAmps(self, vin, amps): return self.module.setAmps(vin, amps)

def make(settings=None, *providers):
    m = Mock(); m.settings = settings or {}; m.config = {}; m.stats = {"moduleSuccess": {}, "moduleFailures": {}, "moduleDispatch": {}}
    m.vehicleRegistry = VehicleRegistry({}); m.providerOrder = ProviderOrder(m)
    for p in providers: m.providerOrder.register(p)
    return m, VehiclePriority(m)

def mod(**results):
    x = Mock(spec=list(results)); [setattr(x, k, Mock(return_value=v)) for k, v in results.items()]; return x

def test_command_policy_properties_delegate():
    m, vp = make({"commandPolicy": "ble_only"})
    assert vp.commandPolicy == "ble_only" and vp.commandPolicyOverridden is False and vp.updateSettings() is True

def test_no_modules_returns_false():                              # Review Focus 2
    m, vp = make()
    assert vp.startCharging(VIN) is False and vp.car_api_charge({"charge": True}) is False

def test_walk_is_order_position_with_local_three_attempts_and_ha_last():
    ble = mod(startCharging=False); api = mod(startCharging=False); ha = mod(startCharging=True)
    m, vp = make({}, P("HomeAssistant", 6, False, ha), P("FleetAPI", 5, False, api), P("TeslaBLE", 2, True, ble))
    assert vp.startCharging(VIN) is True
    assert (ble.startCharging.call_count, api.startCharging.call_count, ha.startCharging.call_count) == (3, 2, 1)
    assert m.stats["moduleFailures"] == {"TeslaBLE": 1, "FleetAPI": 1} and m.stats["moduleSuccess"] == {"HomeAssistant": 1}

def test_ble_only_skips_cloud_commands_but_not_reads():
    ble = mod(startCharging=False, updateChargeAtHome=False); api = mod(startCharging=True, updateChargeAtHome=True)
    m, vp = make({"commandPolicy": "ble_only"}, P("TeslaBLE", 2, True, ble), P("FleetAPI", 5, False, api))
    assert vp.startCharging(VIN) is False and api.startCharging.call_count == 0
    assert vp.updateChargeAtHome() is True and api.updateChargeAtHome.call_count == 1

def test_api_only_walks_fleet_without_ble():                      # Review Focus 1
    ble = mod(applyChargeLimit=True, updateChargeAtHome=True); api = mod(applyChargeLimit="success", updateChargeAtHome=True)
    m, vp = make({"commandPolicy": "api_only"}, P("TeslaBLE", 2, True, ble), P("FleetAPI", 5, False, api))
    assert vp.applyChargeLimit(limit=80) == "success" and ble.applyChargeLimit.call_count == 0
    assert vp.updateChargeAtHome() is True and ble.updateChargeAtHome.call_count == 0        # reads too: no BLE timeout

def test_broadcast_skips_a_provider_that_is_not_available():
    api = mod(applyChargeLimit="success"); f = P("FleetAPI", 5, False, api); f.available = lambda cap: False    # Owner-only: no command slot
    m, vp = make({}, f)
    assert vp.applyChargeLimit(limit=80) is False and api.applyChargeLimit.call_count == 0 and m.stats["moduleFailures"] == {}

def test_targeted_call_skips_providers_not_ready_for_that_car():
    ble = mod(applyChargeLimit=True); api = mod(applyChargeLimit="success")
    m, vp = make({}, P("TeslaBLE", 2, True, ble, ready=False), P("FleetAPI", 5, False, api))
    m.vehicleRegistry.getOrCreate(VIN, "t")
    assert vp.applyChargeLimit(limit=80, vin=VIN) == "success" and ble.applyChargeLimit.call_count == 0
    assert vp.applyChargeLimit(limit=80) is True and ble.applyChargeLimit.call_count == 1      # broadcast: readiness unknown

def test_set_charge_rate_targets_the_car():
    twc = P("Gen2TWC", 0, True, None, amps=(6, 32)); api = mod(setChargeRate=True)
    m, vp = make({}, twc, P("FleetAPI", 5, False, api, caps={C.SET_AMPS: C.Declaration(C.FLEET_COMMAND)})); m.vehicleRegistry.getOrCreate(VIN, "t")
    car = Mock(VIN=VIN)
    assert vp.setChargeRate(4, car) is True and api.setChargeRate.call_args.args == (4, car)   # below the floor
    assert VehiclePriority.targetVIN("setChargeRate", (10, car), {}) == VIN and VehiclePriority.targetVIN("wakeVehicle", (VIN.lower(),), {}) == VIN
    assert VehiclePriority.targetVIN("car_api_charge", ({"charge": False, "vin": VIN},), {}) == VIN

def test_set_charge_rate_zero_still_reaches_the_vehicle():
    # Mode 2 can queue 0 A; TeslaAPI clamps it to 1 A today. The lookup
    # clamps too, so 0 does not drop every candidate; the module gets 0.
    api = mod(setChargeRate=True)
    m, vp = make({}, P("FleetAPI", 5, False, api, caps={C.SET_AMPS: C.Declaration(C.FLEET_COMMAND)})); m.vehicleRegistry.getOrCreate(VIN, "t")
    car = Mock(VIN=VIN)
    assert vp.setChargeRate(0, car) is True and api.setChargeRate.call_args.args == (0, car)
    assert VehiclePriority.targetAmps("setChargeRate", (0, car), {}) == 1.0
    assert VehiclePriority.targetAmps("setChargeRate", (), {"charge_rate": "x"}) is None

def test_set_amps_dispatches_through_the_order():
    ble = Mock(); ble.setAmps = Mock(return_value=True)
    m, vp = make({}, P("TeslaBLE", 2, True, ble)); m.vehicleRegistry.getOrCreate(VIN, "t")
    assert vp.setAmps(VIN, 12) is True and ble.setAmps.call_args.args == (VIN, 12)
    assert vp.setAmps("5YJ3E1EA1KF000009", 12) is False

def test_queue_dispatches_set_amps():
    from TWCManager.VehicleCommandQueue import VehicleCommandQueue
    vp = Mock(); vp.setAmps = Mock(return_value=True); q = VehicleCommandQueue(lambda: vp)
    assert q._execute({"cmd": "setAmps", "vin": VIN, "amps": 7}) is True and VehicleCommandQueue._key({"cmd": "setAmps", "vin": VIN}) == ("setAmps", VIN)
    assert VehicleCommandQueue(lambda: Mock(spec=[]))._execute({"cmd": "setAmps", "vin": VIN, "amps": 7}) is False   # fallback module without setAmps

def test_same_module_under_two_providers_is_walked_once():
    # Owner and Fleet share the TeslaAPI module: one walk position, two attempts
    api = mod(updateChargeAtHome=False)
    m, vp = make({}, P("OwnerAPI", 4, False, api, caps={}), P("FleetAPI", 5, False, api))
    assert vp.updateChargeAtHome() is False and api.updateChargeAtHome.call_count == 2
    assert m.stats["moduleFailures"] == {"OwnerAPI": 1}

def test_exceptions_count_as_failures_and_dunder_probes_do_not_walk():
    api = Mock(spec=["startCharging"]); api.startCharging = Mock(side_effect=RuntimeError("secret"))
    m, vp = make({}, P("FleetAPI", 5, False, api))
    assert vp.startCharging(VIN) is False and api.startCharging.call_count == 2
    assert m.stats["moduleFailures"] == {"FleetAPI": 1}
    assert not hasattr(vp, "__deepcopy__") and not hasattr(vp, "_private")

def test_registration_finds_no_providers_on_the_facade(caplog):
    # registerProviders walks every Vehicle module, VehiclePriority (loaded
    # first) included; the facade must not walk the order for "providers"
    import logging
    from TWCManager.Vehicle.Providers.Order import registerProviders
    ble = Mock(); ble.providers = lambda: [P("TeslaBLE", 2, True, Mock())]
    m, vp = make()
    m.getModulesByType = Mock(return_value=[{"name": "VehiclePriority", "ref": vp}, {"name": "TeslaBLE", "ref": ble}])
    with caplog.at_level(logging.WARNING):
        assert registerProviders(m) == ["TeslaBLE"]
    assert "Could not register" not in caplog.text and vp.providers() == []
    assert "api_only" in VehiclePriority.VALID_COMMAND_POLICIES


def test_rate_raise_reaches_fleet_on_a_fleet_only_install():
    # The TWC cannot lift the car's own current limit (#660): a raise to the
    # wiring max must reach Fleet even though the TWC covers that rate
    twc = P("Gen2TWC", 0, True, None, amps=(6, 32)); api = mod(setChargeRate=True)
    m, vp = make({}, twc, P("FleetAPI", 5, False, api, caps={C.SET_AMPS: C.Declaration(C.FLEET_COMMAND)})); m.vehicleRegistry.getOrCreate(VIN, "t")
    car = Mock(VIN=VIN)
    assert vp.setChargeRate(32, car) is True and api.setChargeRate.call_args.args == (32, car)
    assert vp.setChargeRate(10, car) is True and api.setChargeRate.call_count == 2

def test_set_amps_keeps_the_local_amps_rule():
    twc = P("Gen2TWC", 0, True, None, amps=(6, 32)); api = Mock(); api.setAmps = Mock(return_value=True)
    m, vp = make({}, twc, P("FleetAPI", 5, False, api, caps={C.SET_AMPS: C.Declaration(C.FLEET_COMMAND)})); m.vehicleRegistry.getOrCreate(VIN, "t")
    assert vp.setAmps(VIN, 10) is False and api.setAmps.call_count == 0     # the TWC carries 10 A
    assert vp.setAmps(VIN, 4) is True and api.setAmps.call_args.args == (VIN, 4)


def _haMaster(enabled=True):
    from tests.unit.test_homeassistant_registry import make_ha
    ha, reg = make_ha(enabled=enabled)
    m, vp = make()
    m.vehicleRegistry = reg
    for p in ha.providers():
        m.providerOrder.register(p)
    return m, vp, ha, reg

def test_attempts_match_todays_priorities():
    # TeslaBLE priority 20 -> 3, TeslaAPI 10 -> 2; HomeAssistant has no
    # priority (today it is called once, directly) -> 1
    from tests.unit.test_carapivehicle_registry import api_with_registry
    from tests.unit.teslatokens import fleet_token
    m, vp, ha, reg = _haMaster()
    ha.car_api_charge = Mock(return_value="error")
    api, _ = api_with_registry(); api.storeToken(fleet_token()); api.car_api_charge = Mock(return_value="error")
    for p in api.providers():
        m.providerOrder.register(p)
    assert vp.car_api_charge({"charge": True}) is False
    assert (api.car_api_charge.call_count, ha.car_api_charge.call_count) == (2, 1)
    assert m.stats["moduleFailures"] == {"FleetAPI": 1, "HomeAssistant": 1}

def test_home_assistant_gets_its_own_vehicle_for_set_charge_rate():
    m, vp, ha, reg = _haMaster()
    hv = Mock(has_charge_current_number=True); reg.getOrCreate(VIN, "t").attach("HomeAssistant", hv)
    ha.setChargeRate = Mock(return_value=True); car = Mock(VIN=VIN)
    assert vp.setChargeRate(10, car) is True and ha.setChargeRate.call_args.args == (10, hv)
    other = Mock(VIN="5YJ3E1EA1KF000009")                     # no HA vehicle for it: never the TeslaAPI car
    assert vp.setChargeRate(10, other) is False and ha.setChargeRate.call_count == 1

def test_disabled_home_assistant_gets_no_broadcast():
    m, vp, ha, reg = _haMaster(enabled=False)
    ha.car_api_charge = Mock(return_value="success"); ha.updateChargeAtHome = Mock(return_value=True)
    assert vp.car_api_charge({"charge": True}) is False and ha.car_api_charge.call_count == 0
    assert vp.updateChargeAtHome() is False and ha.updateChargeAtHome.call_count == 0


# Fix round 2

def _fleetUnloaded(withBle):
    # TeslaAPI holds a Fleet token but has not listed its vehicles yet; the
    # registry vehicle came from the TWC, so it has no TeslaAPI attachment
    from tests.unit.test_carapivehicle_registry import api_with_registry
    from tests.unit.teslatokens import fleet_token
    api, reg = api_with_registry(); api.storeToken(fleet_token())
    api.car_api_charge = Mock(return_value="success"); api.applyChargeLimit = Mock(return_value="success")
    m, vp = make(); m.vehicleRegistry = reg
    ble = mod(car_api_charge=False, applyChargeLimit=False)
    for p in ([P("TeslaBLE", 2, True, ble)] if withBle else []) + api.providers():
        m.providerOrder.register(p)
    reg.getOrCreate(VIN, "TWC")
    assert api.getVehicleCount() == 0
    return m, vp, api, ble

def test_failed_ble_stop_falls_back_to_fleet_before_the_list_loads():      # C1
    m, vp, api, ble = _fleetUnloaded(withBle=True)
    assert vp.car_api_charge({"charge": False, "vin": VIN}) == "success"
    assert ble.car_api_charge.call_count == 3 and api.car_api_charge.call_count == 1
    assert vp.applyChargeLimit(limit=80, vin=VIN) == "success" and api.applyChargeLimit.call_count == 1

def test_fleet_only_targeted_commands_work_from_boot():                      # C1
    m, vp, api, ble = _fleetUnloaded(withBle=False)
    api.car_api_charge.return_value = "error"
    assert vp.car_api_charge({"charge": False, "vin": VIN}) is False and api.car_api_charge.call_count == 2
    assert m.stats["moduleFailures"] == {"FleetAPI": 1}

def test_loaded_list_without_the_car_still_skips_fleet():
    m, vp, api, ble = _fleetUnloaded(withBle=False)
    api.addVehicle({"id": 2, "vin": "5YJ3E1EA1KF000009", "display_name": "Other"})
    assert vp.car_api_charge({"charge": False, "vin": VIN}) is False and api.car_api_charge.call_count == 0

OTHER = "5YJ3E1EA1KF000009"

def _haCars(*vins, fleet=True):
    # HA's real car_api_charge/applyChargeLimit over stand-in HA cars, each
    # at home, connected and charging; _call_service records the entities
    m, vp, ha, reg = _haMaster()
    cars = []
    for vin in vins:
        hv = Mock(vin=vin, at_home=True, charging=True, charge_limit=None, has_charge_switch=True, has_charge_limit_number=True,
                  entity_ids={"charge_switch": "switch." + vin, "charge_limit_number": "number." + vin})
        hv.name = vin; cars.append(hv)
        reg.getOrCreate(vin, "t").attach("HomeAssistant", hv)
    ha.carApiVehicles = cars
    ha.car_api_available = Mock(return_value=True); ha._is_vehicle_connected = Mock(return_value=True)
    ha._call_service = Mock(return_value=True)
    api = None
    if fleet:
        api = mod(car_api_charge="error", applyChargeLimit="error")
        m.providerOrder.register(P("FleetAPI", 5, False, api, caps={c: C.Declaration(C.FLEET_COMMAND) for c in C.COMMANDS}))
    return m, vp, ha, api

def _entities(ha):
    return [c.args[2]["entity_id"] for c in ha._call_service.call_args_list]

def test_ha_only_targeted_stop_affects_only_the_target():                   # N1
    m, vp, ha, _ = _haCars(VIN, OTHER, fleet=False)
    assert vp.car_api_charge({"charge": False, "vin": VIN}) == "success" and _entities(ha) == ["switch." + VIN]

def test_fleet_failure_falls_to_home_assistant_for_the_target_only():       # I1
    m, vp, ha, api = _haCars(VIN, OTHER)
    assert vp.car_api_charge({"charge": False, "vin": VIN}) == "success" and api.car_api_charge.call_count == 2
    assert vp.applyChargeLimit(limit=80, vin=VIN) == "success"
    assert _entities(ha) == ["switch." + VIN, "number." + VIN]

def test_broadcast_still_reaches_every_home_assistant_car():
    m, vp, ha, _ = _haCars(VIN, OTHER, fleet=False)
    assert vp.car_api_charge({"charge": False}) == "success"
    assert vp.applyChargeLimit(limit=80) == "success"
    assert _entities(ha) == ["switch." + VIN, "switch." + OTHER, "number." + VIN, "number." + OTHER]

def test_unknown_vin_affects_no_home_assistant_car():
    m, vp, ha, _ = _haCars(VIN, OTHER, fleet=False)
    unknown = "5YJ3E1EA1KF000077"
    assert ha.car_api_charge({"charge": False, "vin": unknown}) == "error"
    assert ha.applyChargeLimit(80, vin=unknown) == "error"
    assert ha._call_service.call_count == 0
    assert ha.car_api_charge({"charge": False, "vin": VIN.lower()}) == "success"     # not rate limited by the refusal
