# tests/unit/test_provider_registration.py
import ast
import logging
import os
from unittest.mock import Mock, patch

import pytest

from TWCManager.TWCMaster import TWCMaster
from TWCManager.Vehicle.Providers import Capability as C
from TWCManager.Vehicle.Providers.HomeAssistantProvider import HomeAssistantProvider
from TWCManager.Vehicle.Providers.Order import ProviderOrder, registerProviders
from TWCManager.Vehicle.Providers.TelemetryProvider import TelemetryProvider
from TWCManager.Vehicle.Registry.VehicleRegistry import VehicleRegistry
from TWCManager.Vehicle.HomeAssistant import HomeAssistant
from TWCManager.Vehicle.TeslaAPI import TeslaAPI
from TWCManager.Vehicle.TeslaBLE import TeslaBLE
from tests.unit.test_gen2twc_provider import CONFIG
from tests.unit.test_homeassistant_registry import make_ha
from tests.unit.test_provider_order import ble as bleStub   # the Phase 1 fixture: (ha, registry)
from tests.unit.test_provider_order import fleet as fleetStub
from tests.unit.test_provider_order import owner as ownerStub
VIN = "5YJ3E1EA1KF000001"
live = {"at": 100.0}
_LIB_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "lib"))

class TeslaMateVehicle:                                   # the provider takes its name from the module's class
    def feedLiveAt(self, vehicle=None): return live["at"]

def test_telemetry_provider_is_passive_free_and_ready_while_live():
    live["at"] = 100.0; p = TelemetryProvider(TeslaMateVehicle())
    assert (p.name, p.rank, p.readSource, p.module) == ("TeslaMateVehicle", 3, None, None)
    assert p.declarations()[C.READ_PLUG] == C.Declaration(C.CLOUD_FREE, passive=True) and p.ready(None, C.READ_PLUG)
    live["at"] = None; assert p.ready(None, C.READ_PLUG) is False

def test_home_assistant_provider_follows_entities():
    ha, reg = make_ha(); (p,) = ha.providers(); v = reg.getOrCreate(VIN, "t")
    assert (p.name, p.rank, p.cost(C.SET_AMPS), p.readSource.provider) == ("HomeAssistant", 6, C.HA_BILLED, "HomeAssistant")
    assert p.ready(v, C.SET_AMPS) is False
    hv = Mock(has_charge_switch=True, has_charge_current_number=False, has_charge_limit_number=True); v.attach("HomeAssistant", hv)
    assert p.ready(v, C.START_CHARGING) and p.ready(v, C.SET_CHARGE_LIMIT) and not p.ready(v, C.SET_AMPS) and not p.declares(C.WAKE)
    ha.setChargeRate = Mock(return_value=True); hv.has_charge_current_number = True
    assert p.setAmps(VIN, 8) is True; ha.setChargeRate.assert_called_once_with(8, vehicle=hv)
    ha.readSource = rebuilt = Mock(); assert p.readSource is rebuilt   # read through, never a stale snapshot

def test_passive_providers_never_enter_the_walk():
    m = Mock(); m.settings = {}; m.config = {}; m.stats = {"moduleSuccess": {}, "moduleFailures": {}}; m.vehicleRegistry = VehicleRegistry({})
    o = ProviderOrder(m); live["at"] = 1.0
    o.register(TelemetryProvider(TeslaMateVehicle()))
    assert o.ordered(C.READ_PLUG) == [] and [p.name for p in o.providers()] == ["TeslaMateVehicle"]

def test_register_providers_collects_every_vehicle_module_in_rank_order():
    m = Mock(); m.settings = {}; m.config = {}; m.stats = {}; m.vehicleRegistry = VehicleRegistry({}); m.providerOrder = ProviderOrder(m)
    ha, reg = make_ha(); ble = Mock(); ble.providers = lambda: [bleStub()]
    m.getModulesByType = Mock(return_value=[{"name": "HomeAssistant", "ref": ha}, {"name": "TeslaBLE", "ref": ble}, {"name": "VehiclePriority", "ref": object()}])
    assert registerProviders(m) == ["TeslaBLE", "HomeAssistant"]      # rank order, not module order


# HomeAssistant comes last, after the free providers and Fleet, and never
# commands under ble_only

def _haMaster(settings):
    m = Mock(); m.settings = settings; m.config = {}; m.stats = {}; m.vehicleRegistry = VehicleRegistry({}); m.providerOrder = ProviderOrder(m)
    ha, reg = make_ha(); api = Mock(); api.providers = lambda: [ownerStub(), fleetStub()]; ble = Mock(); ble.providers = lambda: [bleStub()]
    m.getModulesByType = Mock(return_value=[{"name": "HomeAssistant", "ref": ha}, {"name": "TeslaAPI", "ref": api}, {"name": "TeslaBLE", "ref": ble}])
    registerProviders(m); return m, ha

def test_home_assistant_commands_after_free_providers_and_fleet():
    m, ha = _haMaster({})
    assert [p.name for p in m.providerOrder.ordered(C.SET_AMPS)] == ["TeslaBLE", "FleetAPI", "HomeAssistant"]

def test_ble_only_drops_home_assistant_commands_but_not_its_reads():
    m, ha = _haMaster({"commandPolicy": "ble_only"})
    for cap in (C.SET_AMPS, C.START_CHARGING, C.STOP_CHARGING, C.SET_CHARGE_LIMIT):
        assert "HomeAssistant" not in [p.name for p in m.providerOrder.ordered(cap)]
    assert ha.readSource in m.providerOrder.orderReadSources([ha.readSource])

def test_home_assistant_reads_after_the_fleet_read_source():
    # No reorder and no new paid path: HA read last before, and still does
    m, ha = _haMaster({})
    fleetSource = Mock(provider="FleetAPI", order=1); ownerSource = Mock(provider="OwnerAPI", order=1)
    assert m.providerOrder.orderReadSources([ha.readSource, fleetSource, ownerSource]) == [ownerSource, fleetSource, ha.readSource]


# The Task 5 carry-forward: under api_only the BLE read source is dropped only
# once registerProviders has put TeslaBLE in the order

def _bleMaster(policy):
    config = dict(CONFIG, config=dict(CONFIG["config"], teslaControlPath="/bin/true"), vehicle={"teslaBLE": {"enabled": True}})
    m = TWCMaster("AB", config)
    if policy:
        m.settings["commandPolicy"] = policy
    with patch.object(TeslaBLE, "_start_dbus_session"), patch.object(TeslaBLE, "_cleanup_stale_pipe"):
        ble = TeslaBLE(m)
    m.registerModule({"name": "TeslaBLE", "ref": ble, "type": "Vehicle"})
    ble.get_charge_state = Mock(return_value={"batteryLevel": 50})
    v = m.vehicleRegistry.getOrCreate(VIN, "test"); v.claim("TeslaBLE", True)
    return m, ble, v

def test_api_only_without_registered_providers_still_reads_over_ble():
    # Why startup must call registerProviders: the read source no longer
    # checks commandPolicy itself
    m, ble, v = _bleMaster("api_only")
    v.read("batteryLevel", maxAge=0)
    ble.get_charge_state.assert_called()

def test_api_only_after_register_providers_never_reads_over_ble():
    m, ble, v = _bleMaster("api_only")
    assert registerProviders(m) == ["Gen2TWC", "TeslaBLE"]
    assert ble.readSource not in m.vehicleRegistry.readSources()
    v.read("batteryLevel", maxAge=0)
    ble.get_charge_state.assert_not_called()

@pytest.mark.parametrize("policy", [None, "prefer_ble", "ble_only"])
def test_other_policies_keep_reading_over_ble(policy):
    m, ble, v = _bleMaster(policy)
    registerProviders(m)
    assert m.vehicleRegistry.readSources()[0] is ble.readSource
    assert v.read("batteryLevel", maxAge=0).value == 50
    ble.get_charge_state.assert_called_once_with(VIN)

def test_no_vehicle_modules_registers_only_gen2():
    m = TWCMaster("AB", CONFIG)
    assert registerProviders(m) == ["Gen2TWC"]
    # The distributor runs only beside a controller other than Gen2TWCs
    assert [c["name"] for c in m.getModulesByType("EVSEController")] == ["Gen2TWCs"]

def test_default_boot_registers_unconfigured_modules_that_never_command():
    # A config with no vehicle sections still loads TeslaAPI and HomeAssistant
    # (the module loop's enabled default); neither may reach a car or a site
    m = TWCMaster("AB", CONFIG)
    for name, cls in (("TeslaAPI", TeslaAPI), ("HomeAssistant", HomeAssistant)):
        m.registerModule({"name": name, "ref": cls(m), "type": "Vehicle"})
    assert registerProviders(m) == ["Gen2TWC", "OwnerAPI", "FleetAPI", "HomeAssistant"]
    v = m.vehicleRegistry.getOrCreate(VIN, "test")
    for cap in sorted(C.COMMANDS):
        assert [p.name for p in m.providerOrder.candidates(v, cap) if not p.local] == []
    assert m.providerOrder.candidates(None, C.READ_WALL_CONNECTOR_VINS) == []
    assert [c["name"] for c in m.getModulesByType("EVSEController")] == ["Gen2TWCs"]

def test_one_failing_module_does_not_stop_the_others(caplog):
    m = Mock(); m.settings = {}; m.config = {}; m.stats = {}; m.vehicleRegistry = VehicleRegistry({}); m.providerOrder = ProviderOrder(m)
    ha, reg = make_ha(); ble = Mock(); ble.providers = lambda: [bleStub()]
    broken = Mock(); broken.providers = Mock(side_effect=RuntimeError("boom"))
    m.getModulesByType = Mock(return_value=[{"name": "Broken", "ref": broken}, {"name": "HomeAssistant", "ref": ha}, {"name": "TeslaBLE", "ref": ble}])
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        assert registerProviders(m) == ["TeslaBLE", "HomeAssistant"]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and r.name.endswith("Vehicle.Providers")]
    assert len(warnings) == 1 and "Broken" in warnings[0].getMessage()


# TWCManager.py starts the app at import, so its wiring is read from source

def _launcher():
    with open(os.path.join(_LIB_DIR, "TWCManager", "TWCManager.py")) as f:
        return f.read()

def test_startup_registers_providers_after_the_module_loop():
    src = _launcher()
    loop = src.index("for module in modules_available:")
    call = src.index("registerProviders(master)")
    teslaAPI = src.index('if master.getModuleByName("TeslaAPI") is not None:')
    assert loop < call < teslaAPI and src.count("registerProviders(master)") == 1

def test_startup_notice_runs_right_after_load_settings():
    src = _launcher()
    body = [ast.get_source_segment(src, n) for n in ast.parse(src).body]
    i = body.index("master.loadSettings()")
    assert body[i + 1] == "master.providerOrder.suppressions.noticeOnce()"
    assert sum("noticeOnce" in b for b in body) == 1
